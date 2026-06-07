"""The `systems.*` MCP tools (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.planes.systems``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.state import AllocationState, IllegalTransition, SystemState
from kdive.jobs import queue
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
from kdive.planes.systems import TERMINAL_SYSTEM as _TERMINAL_SYSTEM
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    reject_rootfs_upload_without_window,
    validate_profile,
)
from kdive.security import audit
from kdive.security.context import RequestContext
from kdive.security.rbac import Role, require_role


def _envelope_for_system(system: System) -> ToolResponse:
    """Render a System; ``failed`` becomes a failure envelope (its value is a failure status)."""
    if system.state is SystemState.FAILED:
        return ToolResponse.failure(
            str(system.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": system.state.value},
        )
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data={"project": system.project},
    )


def _defined_envelope(system: System) -> ToolResponse:
    return ToolResponse.success(
        str(system.id),
        SystemState.DEFINED.value,
        suggested_next_actions=["artifacts.create_system_upload", "systems.provision_defined"],
        data={"project": system.project},
    )


async def get_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Return a System the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
        if system is None or system.project not in ctx.projects:
            return _config_error(system_id)
        require_role(ctx, system.project, Role.VIEWER)
        return _envelope_for_system(system)


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    return job_envelope(job, "system_id", system_id)


# System states that occupy a per-project quota slot (terminal torn_down/failed do not).
_NON_TERMINAL_SYSTEM = (
    SystemState.DEFINED,  # the create-without-provision producer (systems.define, #111)
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)


class _CreateLane(Enum):
    PROVISION_NOW = "provision_now"
    DEFINE_ONLY = "define_only"


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


async def provision_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    profile: dict[str, Any],
) -> ToolResponse:
    """Mint a System for a ``granted`` Allocation and enqueue its provision job."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
    except CategorizedError as exc:
        return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _create_from_allocation_locked(
                pool, ctx, uid, parsed, lane=_CreateLane.PROVISION_NOW
            )
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)


async def _create_from_allocation_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
    profile: ProvisioningProfile,
    *,
    lane: _CreateLane,
) -> ToolResponse:
    # Resolve the allocation's project (immutable) before locking so the PROJECT lock key
    # is known up front; a missing/foreign allocation is a not-found-shaped config error.
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        return _config_error(str(alloc_id))
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
            return _config_error(str(alloc_id))
        require_role(ctx, alloc.project, Role.OPERATOR)
        existing = await _find_system_for_allocation(conn, alloc_id)
        if existing is not None:
            return await _existing_create_response(conn, ctx, alloc, existing, lane)
        return await _insert_created_system(conn, ctx, alloc, profile, lane)


async def _existing_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System,
    lane: _CreateLane,
) -> ToolResponse:
    if lane is _CreateLane.DEFINE_ONLY:
        if existing.state is SystemState.DEFINED:
            return _defined_envelope(existing)  # idempotent re-define
        return _config_error(str(existing.id), data={"current_status": existing.state.value})
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
        {"system_id": str(existing.id)},
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return _system_job_envelope(job, existing.id)


async def _admit_defined(
    conn: AsyncConnection, ctx: RequestContext, alloc: Allocation, system: System
) -> ToolResponse:
    """Drive a ``defined`` System ``defined -> provisioning`` and enqueue its provision job.

    The stored profile is provisioned (ADR-0025 decision 7); the Allocation is already
    ``active`` (flipped at ``define``), so it is not touched. Keyed on the allocation, like
    the create lane, so a retried ``systems.provision`` dedups to the same job.
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
        {"system_id": str(system.id)},
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return _system_job_envelope(job, system.id)


async def provision_defined_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Admit a ``defined`` System after its upload window is complete."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as probe:
            probe_system = await SYSTEMS.get(probe, uid)
            if probe_system is None or probe_system.project not in ctx.projects:
                return _config_error(system_id)
            project = probe_system.project
            allocation_id = probe_system.allocation_id
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.PROJECT, project),
            advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            alloc = await ALLOCATIONS.get(conn, system.allocation_id)
            if alloc is None or alloc.project != system.project:
                return _config_error(str(system.allocation_id))
            if system.state in _TERMINAL_SYSTEM:
                return _config_error(system_id, data={"current_status": system.state.value})
            if system.state is SystemState.DEFINED:
                if alloc.state is not AllocationState.ACTIVE:
                    return _config_error(str(alloc.id), data={"current_status": alloc.state.value})
                return await _admit_defined(conn, ctx, alloc, system)
            job = await queue.enqueue(
                conn,
                JobKind.PROVISION,
                {"system_id": str(system.id)},
                job_authorizing(ctx, system.project),
                f"{system.allocation_id}:provision",
            )
            return _system_job_envelope(job, system.id)


async def _insert_created_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    lane: _CreateLane,
) -> ToolResponse:
    """Insert a System, activate its Allocation, and apply the lane's visible delta."""
    if lane is _CreateLane.PROVISION_NOW:
        try:
            reject_rootfs_upload_without_window(profile)
        except CategorizedError as exc:
            return ToolResponse.failure(str(alloc.id), exc.category)
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
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    system_state = (
        SystemState.PROVISIONING if lane is _CreateLane.PROVISION_NOW else SystemState.DEFINED
    )
    tool = "systems.provision" if lane is _CreateLane.PROVISION_NOW else "systems.define"
    transition = "->provisioning" if lane is _CreateLane.PROVISION_NOW else "->defined"
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
            state=system_state,
            provisioning_profile=profile.model_dump(by_alias=True),
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
    if lane is _CreateLane.DEFINE_ONLY:
        return _defined_envelope(system)
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        {"system_id": str(system.id)},
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return _system_job_envelope(job, system.id)


async def define_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    profile: dict[str, Any],
) -> ToolResponse:
    """Create a System in ``defined`` for a ``granted`` Allocation (ADR-0025 decision 10).

    The create-without-provision producer: it opens the rootfs-upload window (ADR-0048 §5).
    Validates the profile (``upload`` rootfs is admitted here — this is the one tool that
    opens an upload window), then under the per-allocation lock inserts the System at
    ``defined`` and flips the Allocation ``granted -> active``. Operator only. Returns a
    System envelope (no job — define does no provider work).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
    except CategorizedError as exc:
        return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _create_from_allocation_locked(
                pool, ctx, uid, parsed, lane=_CreateLane.DEFINE_ONLY
            )
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)
