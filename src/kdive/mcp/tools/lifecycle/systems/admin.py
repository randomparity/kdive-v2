"""Destructive system administration MCP handlers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DestructiveJobKind, Job, JobKind, System
from kdive.domain.state import IllegalTransition, RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import ReprovisionPayload, SystemPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools._common import stale_handle as _stale_handle
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    destructive_opt_in,
    dump_profile,
    profile_digest,
    reject_rootfs_upload_without_window,
)
from kdive.profiles.types import ProvisioningProfileInput
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role
from kdive.services.systems.validation import (
    RootfsValidator,
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

_NON_TERMINAL_RUN = frozenset({RunState.CREATED, RunState.RUNNING})
_REPROVISION = JobKind.REPROVISION
_TEARDOWN = JobKind.TEARDOWN


@dataclass(frozen=True, slots=True)
class SystemAdminHandlers:
    """Destructive system handlers with provider validation seams bound at construction."""

    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    async def reprovision_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
        profile: ProvisioningProfileInput,
    ) -> ToolResponse:
        """Reprovision a `ready` System in place under the same Allocation."""
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        try:
            parsed = ProvisioningProfile.parse(profile)
            validate_profile_for_provider(parsed, self.component_sources)
            reject_rootfs_upload_without_window(parsed)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(system_id, exc)
        with bind_context(principal=ctx.principal):
            try:
                return await _reprovision_locked(pool, ctx, uid, parsed, self.rootfs_validator)
            except IllegalTransition:
                async with pool.connection() as conn:
                    latest = await SYSTEMS.get(conn, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return _config_error(system_id, data=data)


async def _reprovision_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: UUID,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
    ):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.project not in ctx.projects:
            return _config_error(str(system_id))
        allocation = await ALLOCATIONS.get(conn, system.allocation_id)
        if allocation is None or allocation.project not in ctx.projects:
            return _config_error(str(system_id))
        op = DestructiveOp(kind=_REPROVISION, profile_opt_in=_reprovision_opt_in(profile))
        try:
            assert_destructive_allowed(ctx, allocation, op, required_role=Role.OPERATOR)
        except DestructiveOpDenied as denied:
            await _audit_destructive_denied(conn, ctx, system, _REPROVISION, denied.missing)
            return ToolResponse.failure(str(system_id), ErrorCategory.AUTHORIZATION_DENIED)
        digest = profile_digest(profile)
        dedup_key = f"{system_id}:reprovision:{digest}"
        if system.state is SystemState.REPROVISIONING:
            existing = await _job_for_dedup_key(conn, dedup_key)
            if existing is not None:
                return job_envelope(existing, "system_id", system_id)
            return _config_error(str(system_id), data={"current_status": system.state.value})
        if system.state is not SystemState.READY:
            return _config_error(str(system_id), data={"current_status": system.state.value})
        if await _has_live_run(conn, system_id):
            return _stale_handle(str(system_id), current_status=system.state.value)
        try:
            validate_rootfs_for_provider(profile, rootfs_validator)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(system_id), exc)
        return await _admit_reprovision(conn, ctx, system, profile, digest, dedup_key)


def _reprovision_opt_in(profile: ProvisioningProfile) -> bool:
    """Resolve the gate's profile opt-in factor from the target profile."""
    return destructive_opt_in(profile, _REPROVISION)


def _teardown_opt_in(profile: ProvisioningProfile) -> bool:
    return destructive_opt_in(profile, _TEARDOWN)


async def _audit_destructive_denied(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    op_kind: DestructiveJobKind,
    missing: list[str],
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=f"systems.{op_kind.value}",
            object_kind="systems",
            object_id=system.id,
            transition=f"{op_kind.value}:denied",
            args={"system_id": str(system.id), "missing": missing},
            project=system.project,
        ),
    )


async def _has_live_run(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM runs WHERE system_id = %s AND state = ANY(%s) LIMIT 1",
            (system_id, [s.value for s in _NON_TERMINAL_RUN]),
        )
        return await cur.fetchone() is not None


async def _job_for_dedup_key(conn: AsyncConnection, dedup_key: str) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    return Job.model_validate(row) if row else None


async def _admit_reprovision(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    profile: ProvisioningProfile,
    digest: str,
    dedup_key: str,
) -> ToolResponse:
    """Transition ready->reprovisioning, write the new profile, enqueue the keyed job."""
    await SYSTEMS.update_state(conn, system.id, SystemState.REPROVISIONING)
    await conn.execute(
        "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
        (Jsonb(dump_profile(profile)), system.id),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.reprovision",
            object_kind="systems",
            object_id=system.id,
            transition="ready->reprovisioning",
            args={"system_id": str(system.id), "profile_digest": digest},
            project=system.project,
        ),
    )
    job = await queue.enqueue(
        conn,
        JobKind.REPROVISION,
        ReprovisionPayload(system_id=str(system.id), profile_digest=digest),
        job_authorizing(ctx, system.project),
        dedup_key,
    )
    return job_envelope(job, "system_id", system.id)


async def teardown_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Enqueue an idempotent teardown for a System the caller's project owns."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            allocation = await ALLOCATIONS.get(conn, system.allocation_id)
            if allocation is None or allocation.project not in ctx.projects:
                return _config_error(system_id)
            try:
                profile = ProvisioningProfile.parse(system.provisioning_profile)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            op = DestructiveOp(kind=_TEARDOWN, profile_opt_in=_teardown_opt_in(profile))
            try:
                assert_destructive_allowed(ctx, allocation, op, required_role=Role.ADMIN)
            except DestructiveOpDenied as denied:
                await _audit_destructive_denied(conn, ctx, system, _TEARDOWN, denied.missing)
                return ToolResponse.failure(system_id, ErrorCategory.AUTHORIZATION_DENIED)
            if system.state is SystemState.TORN_DOWN:
                return ToolResponse.success(
                    system_id,
                    "torn_down",
                    suggested_next_actions=["systems.get"],
                    data={"project": system.project},
                )
            job = await queue.enqueue(
                conn,
                JobKind.TEARDOWN,
                SystemPayload(system_id=str(uid)),
                job_authorizing(ctx, system.project),
                f"{uid}:teardown",
            )
        return job_envelope(job, "system_id", uid)
