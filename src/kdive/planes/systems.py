"""Worker handlers for the `systems.*` plane."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, Sensitivity, System
from kdive.domain.state import IllegalTransition, SystemState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ReprovisionPayload, SystemPayload, load_payload
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    profile_digest,
    rootfs_upload_window_allowed,
)
from kdive.providers.composition import ProviderRuntime, build_default_provider_runtime
from kdive.providers.local_libvirt.provisioning import domain_name_for
from kdive.providers.ports import Provisioner
from kdive.security import audit
from kdive.store import objectstore as _objectstore
from kdive.store.objectstore import (
    StoredArtifact,
    artifact_key,
    register_artifact_row,
)

_log = logging.getLogger(__name__)

TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})


def object_store_from_env() -> _objectstore.ObjectStore:
    return _objectstore.object_store_from_env()


async def audit_transition(
    conn: AsyncConnection, job: Job, *, project: str, object_id: UUID, transition: str, tool: str
) -> None:
    await audit.record(
        conn,
        job_context_from_job(job, project),
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=object_id,
            transition=transition,
            args={"system_id": str(object_id)},
            project=project,
        ),
    )


async def open_billing_interval(conn: AsyncConnection, allocation_id: UUID) -> None:
    """Stamp the allocation's ``active_started_at`` when its first System reaches ``ready``."""
    await conn.execute(
        "UPDATE allocations SET active_started_at = now() "
        "WHERE id = %s AND active_started_at IS NULL",
        (allocation_id,),
    )


async def _commit_uploaded_rootfs(
    conn: AsyncConnection, system: System, profile: ProvisioningProfile
) -> None:
    """Commit the write-once artifacts row for an 'upload'-kind rootfs (ADR-0048 §6)."""
    if not rootfs_upload_window_allowed(profile):
        return
    key = artifact_key("local", "systems", str(system.id), "rootfs")
    head = await asyncio.to_thread(object_store_from_env().head, key)
    if head is None:
        raise CategorizedError(
            "upload-kind rootfs was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system.id)},
        )
    stored = StoredArtifact(key, head.etag, Sensitivity.SENSITIVE, "rootfs")
    await ARTIFACTS.insert(
        conn, register_artifact_row(stored, owner_kind="systems", owner_id=system.id)
    )
    await upload_manifest.delete_manifest(conn, "systems", system.id)


async def _finalize_provision_ready(
    conn: AsyncConnection, job: Job, system: System, profile: ProvisioningProfile
) -> None:
    await _commit_uploaded_rootfs(conn, system, profile)
    await open_billing_interval(conn, system.allocation_id)
    await audit_transition(
        conn,
        job,
        project=system.project,
        object_id=system.id,
        transition="provisioning->ready",
        tool="systems.provision",
    )


async def provision_handler(
    conn: AsyncConnection, job: Job, provisioning: Provisioner
) -> str | None:
    """Define+start the tagged domain and drive the System ``provisioning -> ready``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "provision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    if system.state is not SystemState.PROVISIONING:
        if system.state in TERMINAL_SYSTEM:
            provisioning.teardown(system.domain_name or domain_name_for(system_id))
        return str(system_id)
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    try:
        domain_name = provisioning.provision(system_id, profile)
    except CategorizedError:
        try:
            async with conn.transaction():
                await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)
                await audit_transition(
                    conn,
                    job,
                    project=system.project,
                    object_id=system_id,
                    transition="provisioning->failed",
                    tool="systems.provision",
                )
        except IllegalTransition:
            _log.info("provision of system %s failed but it is already terminal", system_id)
        raise
    current: SystemState | None = None
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM systems WHERE id = %s FOR UPDATE", (system_id,))
            row = await cur.fetchone()
            current = SystemState(row["state"]) if row is not None else None
        if current is SystemState.PROVISIONING:
            await conn.execute(
                "UPDATE systems SET state = %s, domain_name = %s WHERE id = %s",
                (SystemState.READY.value, domain_name, system_id),
            )
            await _finalize_provision_ready(conn, job, system, profile)
    if current in TERMINAL_SYSTEM:
        provisioning.teardown(domain_name)
        _log.info("provision of system %s superseded by teardown; domain reaped", system_id)
    return str(system_id)


async def reprovision_handler(
    conn: AsyncConnection, job: Job, provisioning: Provisioner
) -> str | None:
    """Apply the new profile in place and drive ``reprovisioning -> ready`` or failed."""
    system_id = UUID(load_payload(job, ReprovisionPayload).system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "reprovision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    if system.state is not SystemState.REPROVISIONING:
        return str(system_id)
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    try:
        domain_name = provisioning.reprovision(system_id, profile)
    except CategorizedError:
        try:
            async with conn.transaction():
                await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)
                await audit_transition(
                    conn,
                    job,
                    project=system.project,
                    object_id=system_id,
                    transition="reprovisioning->failed",
                    tool="systems.reprovision",
                )
        except IllegalTransition:
            _log.info("reprovision of system %s failed but it is already terminal", system_id)
        raise
    fingerprint = profile_digest(profile)
    current: SystemState | None = None
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM systems WHERE id = %s FOR UPDATE", (system_id,))
            row = await cur.fetchone()
            current = SystemState(row["state"]) if row is not None else None
            if current is SystemState.REPROVISIONING:
                await cur.execute(
                    "UPDATE systems SET state = %s, domain_name = %s, "
                    "target_fingerprint = %s WHERE id = %s",
                    (SystemState.READY.value, domain_name, fingerprint, system_id),
                )
        if current is SystemState.REPROVISIONING:
            await audit_transition(
                conn,
                job,
                project=system.project,
                object_id=system_id,
                transition="reprovisioning->ready",
                tool="systems.reprovision",
            )
    return str(system_id)


async def teardown_handler(
    conn: AsyncConnection, job: Job, provisioning: Provisioner
) -> str | None:
    """Destroy+undefine the domain and drive the System ``-> torn_down``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            return None
        domain_name = system.domain_name or domain_name_for(system_id)
        if system.state is not SystemState.TORN_DOWN:
            old = system.state
            await SYSTEMS.update_state(conn, system_id, SystemState.TORN_DOWN)
            await audit_transition(
                conn,
                job,
                project=system.project,
                object_id=system_id,
                transition=f"{old.value}->torn_down",
                tool="systems.teardown",
            )
    provisioning.teardown(domain_name)
    return str(system_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    provisioning: Provisioner | None = None,
    provider_runtime: ProviderRuntime | None = None,
) -> None:
    """Bind the `provision`/`teardown`/`reprovision` job handlers."""
    runtime = provider_runtime or build_default_provider_runtime()
    prov = provisioning or runtime.provisioner()

    async def _provision(conn: AsyncConnection, job: Job) -> str | None:
        return await provision_handler(conn, job, prov)

    async def _teardown(conn: AsyncConnection, job: Job) -> str | None:
        return await teardown_handler(conn, job, prov)

    async def _reprovision(conn: AsyncConnection, job: Job) -> str | None:
        return await reprovision_handler(conn, job, prov)

    registry.register(JobKind.PROVISION, _provision)
    registry.register(JobKind.TEARDOWN, _teardown)
    registry.register(JobKind.REPROVISION, _reprovision)
