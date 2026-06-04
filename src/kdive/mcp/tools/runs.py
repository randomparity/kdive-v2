"""The `runs.*` MCP tools — the Run join-point (ADR-0026).

`runs.create` binds a Run to a `ready` System (whose Allocation must be `active`, fixing
the Run's Allocation per the binding invariant) and an Investigation, and flips the
Investigation `open -> active` on its first Run — all in one transaction holding a
per-System then per-Investigation advisory lock (the global ALLOCATION→SYSTEM→
INVESTIGATION→RUN order). `runs.get` renders a Run; a `failed` Run maps to a failure
envelope carrying the Run's own `failure_category`. RBAC: `create` requires `operator`;
`get` requires project membership. Authz denials raise (ADR-0020: no authz ErrorCategory).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Investigation, Job, JobKind, Run
from kdive.domain.state import (
    AllocationState,
    IllegalTransition,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.profiles.build import BuildProfile
from kdive.providers.local_libvirt.build import Builder, BuildOutput, LocalLibvirtBuild
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

_RUN_HOSTABLE = frozenset({SystemState.READY})
_SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
_ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
_INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _stale_handle(object_id: str, *, current_status: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.STALE_HANDLE, data={"current_status": current_status}
    )


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_run(run: Run) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`."""
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(str(run.id), category, data={"current_status": run.state.value})
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    else:
        actions = ["runs.get"]
    return ToolResponse.success(
        str(run.id),
        run.state.value,
        suggested_next_actions=actions,
        data={"project": run.project},
    )


async def get_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Return a Run the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
        if run is None or run.project not in ctx.projects:
            return _config_error(run_id)
        return _envelope_for_run(run)


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv_uid: UUID,
    sys_uid: UUID,
    build_profile: dict[str, Any],
    *,
    project: str,
) -> ToolResponse:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, sys_uid),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, inv_uid),
    ):
        system = await SYSTEMS.get(conn, sys_uid)
        if system is None:
            return _config_error(str(sys_uid))
        if system.state in _SYSTEM_GONE:
            return _stale_handle(str(sys_uid), current_status=system.state.value)
        if system.state not in _RUN_HOSTABLE:
            return _config_error(str(sys_uid), data={"current_status": system.state.value})
        inv = await _investigation_for_update(conn, inv_uid)
        if inv is None:
            return _config_error(str(inv_uid))
        if inv.state not in _INVESTIGATION_OPEN_FOR_RUN:
            return _config_error(str(inv_uid), data={"current_status": inv.state.value})
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                investigation_id=inv_uid,
                system_id=sys_uid,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="runs.create",
            object_kind="runs",
            object_id=run.id,
            transition="->created",
            args={"investigation_id": str(inv_uid), "system_id": str(sys_uid)},
            project=project,
        )
        if inv.state is InvestigationState.OPEN:
            await INVESTIGATIONS.update_state(conn, inv_uid, InvestigationState.ACTIVE)
            await audit.record(
                conn,
                ctx,
                tool="runs.create",
                object_kind="investigations",
                object_id=inv_uid,
                transition="open->active",
                args={"investigation_id": str(inv_uid)},
                project=project,
            )
        await conn.execute(
            "UPDATE investigations SET last_run_at = now() WHERE id = %s", (inv_uid,)
        )
    return ToolResponse.success(
        str(run.id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data={
            "project": project,
            "investigation_id": str(inv_uid),
            "system_id": str(sys_uid),
        },
    )


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
    system_id: str,
    build_profile: dict[str, Any],
) -> ToolResponse:
    """Bind a Run to a `ready` System + an Investigation; flip `open -> active` on the first Run."""
    inv_uid = _as_uuid(investigation_id)
    if inv_uid is None:
        return _config_error(investigation_id)
    sys_uid = _as_uuid(system_id)
    if sys_uid is None:
        return _config_error(system_id)
    if not isinstance(build_profile, dict):
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, inv_uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            system = await SYSTEMS.get(conn, sys_uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.project != inv.project:
                return _config_error(system_id)
            alloc = await ALLOCATIONS.get(conn, system.allocation_id)
            if alloc is None or alloc.state not in _ALLOC_HOSTABLE:
                current = alloc.state.value if alloc is not None else "missing"
                return _stale_handle(system_id, current_status=current)
            return await _create_locked(
                conn, ctx, inv_uid, sys_uid, build_profile, project=inv.project
            )


_RUN_BUILD_TERMINAL = frozenset({RunState.FAILED, RunState.CANCELED})


def _authorizing(ctx: RequestContext, project: str) -> dict[str, Any]:
    """The job's authorizing tuple (ADR-0027); mirrors `systems._authorizing`."""
    return {"principal": ctx.principal, "agent_session": ctx.agent_session, "project": project}


def _ctx_from_job(job: Job, project: str) -> RequestContext:
    """Reconstruct an attribution context from a job's authorizing tuple (handler audit)."""
    auth = job.authorizing
    agent_session: str | None = auth.get("agent_session")
    return RequestContext(
        principal=str(auth["principal"]),
        agent_session=agent_session,
        projects=(project,),
        roles={},
    )


def _run_job_envelope(job: Job, run_id: UUID) -> ToolResponse:
    """A job-handle envelope (like `from_job`) carrying the Run id in ``data``."""
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, "run_id": str(run_id)}})


async def _enqueue_build(conn: AsyncConnection, ctx: RequestContext, run: Run) -> Job:
    return await queue.enqueue(
        conn,
        JobKind.BUILD,
        {"run_id": str(run.id)},
        _authorizing(ctx, run.project),
        f"{run.id}:build",
    )


async def _build_locked(conn: AsyncConnection, ctx: RequestContext, run: Run) -> ToolResponse:
    """Admit the build under the per-Run lock: flip `created → running`, then enqueue."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state in _RUN_BUILD_TERMINAL:
            return _config_error(str(run.id), data={"current_status": state.value})
        if state is RunState.CREATED:
            await conn.execute(
                "UPDATE runs SET state = 'running' WHERE id = %s AND state = 'created'", (run.id,)
            )
            await audit.record(
                conn,
                ctx,
                tool="runs.build",
                object_kind="runs",
                object_id=run.id,
                transition="created->running",
                args={"run_id": str(run.id)},
                project=run.project,
            )
        job = await _enqueue_build(conn, ctx, run)
    return _run_job_envelope(job, run.id)


async def build_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent build for a Run: drive `created → running` and enqueue the job."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            try:
                BuildProfile.parse(run.build_profile)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)
            return await _build_locked(conn, ctx, run)


async def _existing_build_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the recorded `(run_id, "build")` ledger result, or ``None`` (short read)."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None
    result = row["result"]
    return result if isinstance(result, dict) else None


async def _finalize_build(
    conn: AsyncConnection, job: Job, run: Run, result: dict[str, Any]
) -> None:
    """Record the build ledger row and drive `running → succeeded` under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result)),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None or RunState(row["state"]) is not RunState.RUNNING:
            return  # already finalized (succeeded) or superseded (canceled) — no-op
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'running'",
            (result["kernel_ref"], result["debuginfo_ref"], run.id),
        )
        await audit.record(
            conn,
            _ctx_from_job(job, run.project),
            tool="runs.build",
            object_kind="runs",
            object_id=run.id,
            transition="running->succeeded",
            args={"run_id": str(run.id)},
            project=run.project,
        )


async def _fail_build(conn: AsyncConnection, job: Job, run: Run, category: ErrorCategory) -> None:
    """Drive `running → failed` with ``category``; tolerate a concurrent cancel."""
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            await conn.execute(
                "UPDATE runs SET state = 'failed', failure_category = %s "
                "WHERE id = %s AND state = 'running'",
                (category.value, run.id),
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run.id,))
                row = await cur.fetchone()
            if row is None or RunState(row["state"]) is not RunState.FAILED:
                raise IllegalTransition(f"run {run.id} was not running at build failure")
            await audit.record(
                conn,
                _ctx_from_job(job, run.project),
                tool="runs.build",
                object_kind="runs",
                object_id=run.id,
                transition="running->failed",
                args={"run_id": str(run.id)},
                project=run.project,
            )
    except IllegalTransition:
        _log.warning(
            "build of run %s failed (%s) but it is already terminal; failure not recorded "
            "on the Run (a concurrent cancel won)",
            run.id,
            category.value,
        )


async def build_handler(conn: AsyncConnection, job: Job, builder: Builder) -> str | None:
    """Build the Run's kernel and drive it `running → succeeded` (or `-> failed`).

    The build (`make` + the two artifact puts) runs with **no DB transaction held** (the
    worker contract). The ledger record and the Run finalize commit together in one short
    transaction under the per-Run lock; a re-dispatch with a recorded ledger row skips the
    rebuild (ADR-0027). On a build failure the Run is driven `failed` with the build's
    category and the error re-raised so the worker dead-letters the job.
    """
    run_id = UUID(job.payload["run_id"])
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "build target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    profile = BuildProfile.parse(run.build_profile)
    result = await _existing_build_result(conn, run_id)
    if result is None:
        try:
            output: BuildOutput = await asyncio.to_thread(builder.build, run_id, profile)
        except CategorizedError as exc:
            await _fail_build(conn, job, run, exc.category)
            raise
        result = {
            "kernel_ref": output.kernel_ref,
            "debuginfo_ref": output.debuginfo_ref,
            "build_id": output.build_id,
        }
    await _finalize_build(conn, job, run, result)
    return str(run_id)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="runs.get")
    async def runs_get(run_id: str) -> ToolResponse:
        return await get_run(pool, current_context(), run_id)

    @app.tool(name="runs.create")
    async def runs_create(
        investigation_id: str, system_id: str, build_profile: dict[str, Any]
    ) -> ToolResponse:
        return await create_run(
            pool,
            current_context(),
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
        )

    @app.tool(name="runs.build")
    async def runs_build(run_id: str) -> ToolResponse:
        return await build_run(pool, current_context(), run_id)


def register_handlers(registry: HandlerRegistry, *, builder: Builder | None = None) -> None:
    """Bind the `build` job handler; build the builder lazily from env.

    Building the builder does not spawn ``make`` or open an object-store connection (the
    real ops run only when ``build()`` is called), so the worker boots without a toolchain.
    """
    build = builder or LocalLibvirtBuild.from_env()

    async def _build(conn: AsyncConnection, job: Job) -> str | None:
        return await build_handler(conn, job, build)

    registry.register(JobKind.BUILD, _build)
