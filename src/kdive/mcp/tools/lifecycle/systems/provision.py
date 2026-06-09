"""System define/provision admission handlers (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.planes.systems``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle_rules import TERMINAL_SYSTEM_STATES as _TERMINAL_SYSTEM
from kdive.domain.models import Allocation, JobKind, System
from kdive.domain.state import AllocationState, IllegalTransition, SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import SystemPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    authorizing as job_authorizing,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    job_envelope,
)
from kdive.mcp.tools.lifecycle.systems.common import (
    RootfsValidator,
    _validate_profile_for_provider,
    _validate_rootfs_for_provider,
)
from kdive.mcp.tools.lifecycle.systems.view import defined_system_envelope
from kdive.profiles.provisioning import (
    AllocationSizing,
    ProvisioningProfile,
    dump_profile,
    reconcile_profile_sizing,
    reject_rootfs_upload_without_window,
    require_concrete_sizing,
)
from kdive.profiles.types import ProvisioningProfileInput
from kdive.providers.component_validation import ComponentSourceCapabilities
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

# System states that occupy a per-project quota slot (terminal torn_down/failed do not).
_NON_TERMINAL_SYSTEM = (
    SystemState.DEFINED,  # the create-without-provision producer (systems.define, #111)
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)

type LockedAllocationSystem = tuple[AsyncConnection, Allocation, System | None]
type CreateSystemMode = Literal["provision", "define"]


@dataclass(frozen=True, slots=True)
class MissingAllocation:
    """A not-found or out-of-scope Allocation encountered while acquiring locks."""

    allocation_id: UUID


# Maps the Allocation's GB memory snapshot to the profile's MB sizing (ADR-0067 lossless).
_MB_PER_GB = 1024


def _stored_profile_for(
    profile: ProvisioningProfileInput, alloc: Allocation
) -> ProvisioningProfile:
    """Resolve the concrete profile to store for ``alloc`` (ADR-0067, ADR-0024 delta).

    When the Allocation carries a complete resolved-sizing snapshot (``requested_vcpus`` /
    ``requested_memory_gb`` / ``requested_disk_gb``), the profile sizing is reconciled
    against it — filled when omitted, rejected when conflicting — so admitted size equals
    booted size. When the snapshot is incomplete (a full-custom or legacy allocation), the
    profile must carry its own concrete sizing. Either way the stored profile is concrete,
    so the libvirt renderer never reads a ``None`` size.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on a conflicting restatement or a profile
            with missing sizing in the no-snapshot lane.
    """
    if (
        alloc.requested_vcpus is not None
        and alloc.requested_memory_gb is not None
        and alloc.requested_disk_gb is not None
    ):
        reconciled = reconcile_profile_sizing(
            profile,
            AllocationSizing(
                vcpu=alloc.requested_vcpus,
                memory_mb=alloc.requested_memory_gb * _MB_PER_GB,
                disk_gb=alloc.requested_disk_gb,
            ),
        )
        return ProvisioningProfile.parse(reconciled)
    parsed = ProvisioningProfile.parse(profile)
    require_concrete_sizing(parsed)
    return parsed


@dataclass(frozen=True, slots=True)
class SystemProvisionHandlers:
    """Provisioning handlers with provider validation seams bound at construction."""

    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    async def provision_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
    ) -> ToolResponse:
        """Mint a System for a ``granted`` Allocation and enqueue its provision job."""
        return await self._create_for_allocation(
            pool,
            ctx,
            allocation_id=allocation_id,
            profile=profile,
            mode="provision",
        )

    async def provision_defined_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
    ) -> ToolResponse:
        """Admit a ``defined`` System after its upload window is complete."""
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        with bind_context(principal=ctx.principal):
            return await _provision_defined_locked(
                pool,
                ctx,
                uid,
                component_sources=self.component_sources,
                rootfs_validator=self.rootfs_validator,
            )

    async def define_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
    ) -> ToolResponse:
        """Create a System in ``defined`` for a ``granted`` Allocation."""
        return await self._create_for_allocation(
            pool,
            ctx,
            allocation_id=allocation_id,
            profile=profile,
            mode="define",
        )

    async def _create_for_allocation(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
        mode: CreateSystemMode,
    ) -> ToolResponse:
        """Parse, authorize, and lock the shared create-lane admission path.

        The submitted profile is structurally pre-parsed first (sizing now optional,
        ADR-0067) for early provider/rootfs validation. The sizing is reconciled against the
        Allocation's resolved snapshot inside the lock — once ``alloc`` is in scope — at the
        single create-insert point (:func:`_stored_profile_for`), so the stored profile is
        always concrete and admitted size equals booted size.
        """
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        try:
            parsed = ProvisioningProfile.parse(profile)
            _validate_profile_for_provider(parsed, self.component_sources)
        except CategorizedError as exc:
            return ToolResponse.failure(allocation_id, exc.category)
        with bind_context(principal=ctx.principal):
            try:
                async with _locked_allocation_system(pool, ctx, uid) as locked:
                    if isinstance(locked, MissingAllocation):
                        return _config_error(str(locked.allocation_id))
                    conn, alloc, existing = locked
                    try:
                        stored = _stored_profile_for(profile, alloc)
                    except CategorizedError as exc:
                        return ToolResponse.failure(str(alloc.id), exc.category)
                    if mode == "provision":
                        return await _provision_create_response(
                            conn,
                            ctx,
                            alloc,
                            existing,
                            profile=stored,
                            rootfs_validator=self.rootfs_validator,
                        )
                    return await _define_create_response(
                        conn,
                        ctx,
                        alloc,
                        existing,
                        profile=stored,
                        rootfs_validator=self.rootfs_validator,
                    )
            except IllegalTransition:
                async with pool.connection() as conn:
                    latest = await ALLOCATIONS.get(conn, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return _config_error(allocation_id, data=data)


async def _within_system_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_concurrent_systems`` (ADR-0007 §4).

    Fail-closed: a project with **no quota row** is over quota (no silent default).
    Counts the project's non-terminal Systems under the held PROJECT lock, so the
    count-then-create cannot overshoot under concurrent provisions.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_concurrent_systems FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    if row is None:
        return False
    cap = int(row[0])
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM systems WHERE project = %s AND state = ANY(%s)",
            (project, [s.value for s in _NON_TERMINAL_SYSTEM]),
        )
        count_row = await cur.fetchone()
    if count_row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(count_row[0]) < cap


async def _find_system_for_allocation(conn: AsyncConnection, alloc_id: UUID) -> System | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM systems WHERE allocation_id = %s ORDER BY created_at, id LIMIT 1",
            (alloc_id,),
        )
        row = await cur.fetchone()
    return System.model_validate(row) if row else None


@asynccontextmanager
async def _locked_allocation_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
) -> AsyncIterator[LockedAllocationSystem | MissingAllocation]:
    # Resolve the allocation's project (immutable) before locking so the PROJECT lock key
    # is known up front; a missing/foreign allocation is a not-found-shaped config error.
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        yield MissingAllocation(alloc_id)
        return
    project = probe_alloc.project
    # PROJECT → ALLOCATION (the global lock order, ADR-0040 §1): the project lock so the
    # max_concurrent_systems count-then-create is race-free against a concurrent provision,
    # the allocation lock so a release-mid-provision cannot leak a domain (the M0 invariant).
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id),
    ):
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None or alloc.project not in ctx.projects:
            yield MissingAllocation(alloc_id)
            return
        require_role(ctx, alloc.project, Role.OPERATOR)
        existing = await _find_system_for_allocation(conn, alloc_id)
        yield conn, alloc, existing


async def _provision_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    if existing is None:
        return await _insert_provisioning_system(conn, ctx, alloc, profile, rootfs_validator)
    if existing.state in _TERMINAL_SYSTEM:
        return _config_error(str(existing.id), data={"current_status": existing.state.value})
    if existing.state is SystemState.DEFINED:
        return _config_error(
            str(existing.id),
            data={
                "current_status": existing.state.value,
                "reason": "use_systems.provision_defined",
            },
        )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(existing.id)),
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return job_envelope(job, "system_id", existing.id)


async def _define_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    if existing is None:
        return await _insert_defined_system(conn, ctx, alloc, profile, rootfs_validator)
    if existing.state is SystemState.DEFINED:
        return defined_system_envelope(existing)  # idempotent re-define
    return _config_error(str(existing.id), data={"current_status": existing.state.value})


async def _admit_defined(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    system: System,
) -> ToolResponse:
    """Drive a ``defined`` System ``defined -> provisioning`` and enqueue its provision job.

    The stored profile is provisioned (ADR-0025 decision 7); the Allocation is already
    ``active`` (flipped at ``define``), so it is not touched. Keyed on the allocation, so
    a retried ``systems.provision`` dedups to the same job.
    """
    await SYSTEMS.update_state(conn, system.id, SystemState.PROVISIONING)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.provision",
            object_kind="systems",
            object_id=system.id,
            transition="defined->provisioning",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system.id)),
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return job_envelope(job, "system_id", system.id)


async def _provision_defined_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: UUID,
    *,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    async with pool.connection() as probe:
        probe_system = await SYSTEMS.get(probe, system_id)
        if probe_system is None or probe_system.project not in ctx.projects:
            return _config_error(str(system_id))
        project = probe_system.project
        allocation_id = probe_system.allocation_id
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.project not in ctx.projects:
            return _config_error(str(system_id))
        require_role(ctx, system.project, Role.OPERATOR)
        alloc = await ALLOCATIONS.get(conn, system.allocation_id)
        if alloc is None or alloc.project != system.project:
            return _config_error(str(system.allocation_id))
        return await _provision_defined_response(
            conn,
            ctx,
            system,
            alloc,
            component_sources=component_sources,
            rootfs_validator=rootfs_validator,
        )


async def _provision_defined_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    alloc: Allocation,
    *,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    if system.state in _TERMINAL_SYSTEM:
        return _config_error(str(system.id), data={"current_status": system.state.value})
    try:
        parsed = ProvisioningProfile.parse(system.provisioning_profile)
        _validate_profile_for_provider(parsed, component_sources)
        _validate_rootfs_for_provider(parsed, rootfs_validator)
    except CategorizedError as exc:
        return ToolResponse.failure(str(system.id), exc.category)
    if system.state is SystemState.DEFINED:
        if alloc.state is not AllocationState.ACTIVE:
            return _config_error(str(alloc.id), data={"current_status": alloc.state.value})
        return await _admit_defined(conn, ctx, alloc, system)
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system.id)),
        job_authorizing(ctx, system.project),
        f"{system.allocation_id}:provision",
    )
    return job_envelope(job, "system_id", system.id)


async def _new_system_allowed(
    conn: AsyncConnection,
    alloc: Allocation,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse | None:
    if alloc.state is not AllocationState.GRANTED:
        return _config_error(str(alloc.id), data={"current_status": alloc.state.value})
    # New System: enforce the per-project max_concurrent_systems quota under the held
    # project lock. Fail-closed — no quota row → denied (ADR-0007 §4); a denial writes
    # no System, no job, and leaves the allocation granted (the all-or-nothing rule).
    if not await _within_system_quota(conn, alloc.project):
        return ToolResponse.failure(
            str(alloc.id),
            ErrorCategory.QUOTA_EXCEEDED,
            suggested_next_actions=["systems.get", "allocations.list"],
        )
    try:
        _validate_rootfs_for_provider(profile, rootfs_validator)
    except CategorizedError as exc:
        return ToolResponse.failure(str(alloc.id), exc.category)
    return None


async def _insert_system_and_activate(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    *,
    state: SystemState,
    tool: str,
    transition: str,
) -> System:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=alloc.project,
            allocation_id=alloc.id,
            state=state,
            provisioning_profile=dump_profile(profile),
            shape=alloc.shape,
        ),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=system.id,
            transition=transition,
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.ACTIVE)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="allocations",
            object_id=alloc.id,
            transition="granted->active",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return system


async def _insert_defined_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    blocked = await _new_system_allowed(conn, alloc, profile, rootfs_validator)
    if blocked is not None:
        return blocked
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.DEFINED,
        tool="systems.define",
        transition="->defined",
    )
    return defined_system_envelope(system)


async def _insert_provisioning_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> ToolResponse:
    try:
        reject_rootfs_upload_without_window(profile)
    except CategorizedError as exc:
        return ToolResponse.failure(str(alloc.id), exc.category)
    blocked = await _new_system_allowed(conn, alloc, profile, rootfs_validator)
    if blocked is not None:
        return blocked
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.PROVISIONING,
        tool="systems.provision",
        transition="->provisioning",
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system.id)),
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return job_envelope(job, "system_id", system.id)
