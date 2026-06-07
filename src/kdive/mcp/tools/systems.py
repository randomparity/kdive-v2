"""The `systems.*` MCP tools and the provision/teardown job handlers (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation, flips the Allocation ``granted -> active``, and enqueues a ``provision`` job — all
atomic under a per-allocation advisory lock — then returns a job handle. The ``provision``
handler renders+defines the tagged libvirt domain and drives ``provisioning -> ready`` (or
``-> failed``); the ``teardown`` handler destroys+undefines and drives ``-> torn_down``. Both
serialize their state decision on a per-System lock so a release-mid-provision cannot leak a
domain. Handlers reconstruct a RequestContext from the job's authorizing tuple to audit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.state import AllocationState, IllegalTransition, RunState, SystemState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
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
    context_from_job as job_context_from_job,
)
from kdive.mcp.tools._common import (
    job_envelope,
)
from kdive.mcp.tools._common import (
    stale_handle as _stale_handle,
)
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.composition import (
    reject_rootfs_without_upload_window,
    validate_profile,
)
from kdive.security import audit
from kdive.security.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.rbac import Role, require_role
from kdive.store import objectstore as _objectstore

_log = logging.getLogger(__name__)

_TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})
# Non-terminal Run states that block a reprovision (ADR-0038 §4): a Run bound to the
# System's prior boot is invalid against the new install.
_NON_TERMINAL_RUN = frozenset({RunState.CREATED, RunState.RUNNING})
_REPROVISION = "reprovision"


def object_store_from_env() -> _objectstore.ObjectStore:
    return _objectstore.object_store_from_env()


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
        suggested_next_actions=["artifacts.create_system_upload", "systems.provision"],
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
        return _envelope_for_system(system)


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    return job_envelope(job, "system_id", system_id)


async def _audit_transition(
    conn: AsyncConnection, job: Job, *, project: str, object_id: UUID, transition: str, tool: str
) -> None:
    await audit.record(
        conn,
        job_context_from_job(job, project),
        tool=tool,
        object_kind="systems",
        object_id=object_id,
        transition=transition,
        args={"system_id": str(object_id)},
        project=project,
    )


async def _open_billing_interval(conn: AsyncConnection, allocation_id: UUID) -> None:
    """Stamp the allocation's ``active_started_at`` when its first System reaches ``ready``.

    The active billing interval opens on the ``granted -> active`` edge, defined by the
    first System reaching ``ready`` (ADR-0007 §3); ``active_hours = active_ended_at −
    active_started_at`` prices the reconcile credit, so a never-stamped start would
    reconcile every active allocation at ``active_hours = 0`` and credit back the full
    reservation. First-write-wins (``WHERE active_started_at IS NULL``): a second System on
    the same allocation, a reprovision-in-place cycle, or a handler re-run never slides the
    interval forward. The conditional ``UPDATE`` runs in the caller's per-System transaction.
    """
    await conn.execute(
        "UPDATE allocations SET active_started_at = now() "
        "WHERE id = %s AND active_started_at IS NULL",
        (allocation_id,),
    )


# System states that occupy a per-project quota slot (terminal torn_down/failed do not).
_NON_TERMINAL_SYSTEM = (
    SystemState.DEFINED,  # the create-without-provision producer (systems.define, #111)
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)


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
    profile: dict[str, Any] | None,
) -> ToolResponse:
    """Mint or admit a System for a ``granted`` Allocation and enqueue its provision job.

    Create lane (no System yet): ``profile`` is required; an ``upload`` rootfs is rejected
    (no upload window). Admit lane (a ``defined`` System exists): ``profile`` is ignored and
    the stored profile is provisioned (ADR-0025 decisions 7, 10).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    parsed: ProvisioningProfile | None = None
    if profile is not None:
        try:
            parsed = ProvisioningProfile.parse(profile)
            validate_profile(parsed)
        except CategorizedError as exc:
            return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _provision_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)


async def _provision_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
    profile: ProvisioningProfile | None,
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
            if existing.state in _TERMINAL_SYSTEM:
                return _config_error(
                    str(existing.id), data={"current_status": existing.state.value}
                )
            if existing.state is SystemState.DEFINED:
                # Admit advances state (defined->provisioning), so — like the create lane's
                # `granted` check — it must refuse a System whose lease is no longer active
                # (released/expired before the reconciler reaped it); otherwise it would
                # drive a doomed System into provisioning and spawn a provider job.
                if alloc.state is not AllocationState.ACTIVE:
                    return _config_error(str(alloc_id), data={"current_status": alloc.state.value})
                return await _admit_defined(conn, ctx, alloc, existing)
            job = await queue.enqueue(
                conn,
                JobKind.PROVISION,
                {"system_id": str(existing.id)},
                job_authorizing(ctx, alloc.project),
                f"{alloc_id}:provision",
            )
            return _system_job_envelope(job, existing.id)
        if profile is None:
            return _config_error(str(alloc_id), data={"reason": "profile_required"})
        return await _create_provisioning_system(conn, ctx, alloc, profile)


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
        tool="systems.provision",
        object_kind="systems",
        object_id=system.id,
        transition="defined->provisioning",
        args={"allocation_id": str(alloc.id)},
        project=alloc.project,
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        {"system_id": str(system.id)},
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return _system_job_envelope(job, system.id)


async def _create_provisioning_system(
    conn: AsyncConnection, ctx: RequestContext, alloc: Allocation, profile: ProvisioningProfile
) -> ToolResponse:
    """Create a new provisioning System, activate its Allocation, and enqueue provision."""
    try:
        reject_rootfs_without_upload_window(profile.provider.local_libvirt.rootfs)
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
            state=SystemState.PROVISIONING,
            provisioning_profile=profile.model_dump(by_alias=True),
        ),
    )
    await audit.record(
        conn,
        ctx,
        tool="systems.provision",
        object_kind="systems",
        object_id=system.id,
        transition="->provisioning",
        args={"allocation_id": str(alloc.id)},
        project=alloc.project,
    )
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.ACTIVE)
    await audit.record(
        conn,
        ctx,
        tool="systems.provision",
        object_kind="allocations",
        object_id=alloc.id,
        transition="granted->active",
        args={"allocation_id": str(alloc.id)},
        project=alloc.project,
    )
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
            return await _define_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)


async def _define_locked(
    pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: UUID, profile: ProvisioningProfile
) -> ToolResponse:
    """Insert a ``defined`` System and flip the Allocation active, under PROJECT->ALLOCATION."""
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        return _config_error(str(alloc_id))
    project = probe_alloc.project
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
            if existing.state is SystemState.DEFINED:
                return _defined_envelope(existing)  # idempotent re-define
            return _config_error(str(existing.id), data={"current_status": existing.state.value})
        if alloc.state is not AllocationState.GRANTED:
            return _config_error(str(alloc_id), data={"current_status": alloc.state.value})
        if not await _within_system_quota(conn, alloc.project):
            return ToolResponse.failure(
                str(alloc_id),
                ErrorCategory.QUOTA_EXCEEDED,
                suggested_next_actions=["systems.get", "allocations.list"],
            )
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
                allocation_id=alloc_id,
                state=SystemState.DEFINED,
                provisioning_profile=profile.model_dump(by_alias=True),
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="systems.define",
            object_kind="systems",
            object_id=system.id,
            transition="->defined",
            args={"allocation_id": str(alloc_id)},
            project=alloc.project,
        )
        await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.ACTIVE)
        await audit.record(
            conn,
            ctx,
            tool="systems.define",
            object_kind="allocations",
            object_id=alloc_id,
            transition="granted->active",
            args={"allocation_id": str(alloc_id)},
            project=alloc.project,
        )
        return _defined_envelope(system)


def _reprovision_opt_in(profile: ProvisioningProfile) -> bool:
    """Resolve the gate's profile opt-in factor from the target profile (ADR-0038 §3)."""
    return _REPROVISION in profile.provider.local_libvirt.destructive_ops


async def _has_live_run(conn: AsyncConnection, system_id: UUID) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM runs WHERE system_id = %s AND state = ANY(%s) LIMIT 1",
            (system_id, [s.value for s in _NON_TERMINAL_RUN]),
        )
        return await cur.fetchone() is not None


async def reprovision_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str, profile: dict[str, Any]
) -> ToolResponse:
    """Reprovision a `ready` System in place under the same Allocation (ADR-0038).

    Validates and digests the target profile, then under the per-System lock gates the op
    (capability scope ∧ profile opt-in ∧ ``operator`` role), refuses a System that is not
    ``ready`` (``configuration_error``) or that has a live Run (``stale_handle``), and on
    success drives ``ready -> reprovisioning`` while writing the new profile to the same
    row and enqueuing a ``reprovision`` job keyed by the profile digest (a same-profile
    re-issue dedups; a different profile is a new job).
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
        reject_rootfs_without_upload_window(parsed.provider.local_libvirt.rootfs)
    except CategorizedError as exc:
        return ToolResponse.failure(system_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _reprovision_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await SYSTEMS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(system_id, data=data)


async def _reprovision_locked(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: UUID, profile: ProvisioningProfile
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
            await audit.record(
                conn,
                ctx,
                tool="systems.reprovision",
                object_kind="systems",
                object_id=system_id,
                transition="reprovision:denied",
                args={"system_id": str(system_id), "missing": denied.missing},
                project=system.project,
            )
            return ToolResponse.failure(str(system_id), ErrorCategory.AUTHORIZATION_DENIED)
        digest = profile_digest(profile)
        dedup_key = f"{system_id}:reprovision:{digest}"
        if system.state is SystemState.REPROVISIONING:
            # A re-issue of the in-flight reprovision dedups to its job; a *different*
            # reprovision while one is in flight is rejected (the System is busy).
            existing = await _job_for_dedup_key(conn, dedup_key)
            if existing is not None:
                return _system_job_envelope(existing, system_id)
            return _config_error(str(system_id), data={"current_status": system.state.value})
        if system.state is not SystemState.READY:
            return _config_error(str(system_id), data={"current_status": system.state.value})
        if await _has_live_run(conn, system_id):
            return _stale_handle(str(system_id), current_status=system.state.value)
        return await _admit_reprovision(conn, ctx, system, profile, digest, dedup_key)


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
        (Jsonb(profile.model_dump(by_alias=True)), system.id),
    )
    await audit.record(
        conn,
        ctx,
        tool="systems.reprovision",
        object_kind="systems",
        object_id=system.id,
        transition="ready->reprovisioning",
        args={"system_id": str(system.id), "profile_digest": digest},
        project=system.project,
    )
    job = await queue.enqueue(
        conn,
        JobKind.REPROVISION,
        {"system_id": str(system.id), "profile_digest": digest},
        job_authorizing(ctx, system.project),
        dedup_key,
    )
    return _system_job_envelope(job, system.id)


async def teardown_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Enqueue an idempotent teardown for a System the caller's project owns (admin).

    Direct teardown of a still-allocated System is a destructive-administration op and
    requires ``admin`` (ADR-0037 §1/§2). An ``operator`` frees the quota it holds by
    ``allocations.release`` instead, which orphans the System for the reconciler's
    principal-less GC teardown (ADR-0021); idle-lease expiry feeds the same path. The role
    check binds to the target System's project, after the in-project check.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.ADMIN)
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
                {"system_id": str(uid)},
                job_authorizing(ctx, system.project),
                f"{uid}:teardown",
            )
        return _system_job_envelope(job, uid)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="systems.define",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_define(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to create a DEFINED System for.")
        ],
        profile: Annotated[
            dict[str, Any],
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation (upload window). Operator only."""
        return await define_system(
            pool, current_context(), allocation_id=allocation_id, profile=profile
        )

    @app.tool(
        name="systems.provision",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Provisioning profile for the create lane (required when no System "
                "exists yet); ignored when admitting an already-defined System.",
            ),
        ] = None,
    ) -> ToolResponse:
        """Mint or admit a System for a granted Allocation and enqueue provision. Operator only."""
        return await provision_system(
            pool, current_context(), allocation_id=allocation_id, profile=profile
        )

    @app.tool(
        name="systems.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_get(
        system_id: Annotated[str, Field(description="The System to render.")],
    ) -> ToolResponse:
        """Render a System; failed maps to a failure envelope. Requires project membership."""
        return await get_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
    ) -> ToolResponse:
        """Enqueue an idempotent teardown for a System; destroys the domain. Requires admin."""
        return await teardown_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            dict[str, Any],
            Field(description="New provisioning profile; must opt in to reprovision."),
        ],
    ) -> ToolResponse:
        """Reprovision a ready System in place under its Allocation. Requires operator + gate."""
        return await reprovision_system(
            pool, current_context(), system_id=system_id, profile=profile
        )
