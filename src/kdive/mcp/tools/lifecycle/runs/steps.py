"""`runs.install` and `runs.boot` MCP handlers."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.models import JobKind, Run
from kdive.domain.state import RunState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.common import run_job_envelope
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role


async def install_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent install for a built, SUCCEEDED Run."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            return await _enqueue_step(conn, ctx, run, JobKind.INSTALL, "install", "runs.install")


async def boot_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent boot for a built, installed Run."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            if not await _has_succeeded_step(conn, uid, "install"):
                return _config_error(run_id, data={"reason": "install_first"})
            return await _enqueue_step(conn, ctx, run, JobKind.BOOT, "boot", "runs.boot")


async def _has_succeeded_step(conn: AsyncConnection, run_id: UUID, step: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s AND state = 'succeeded'",
            (run_id, step),
        )
        return await cur.fetchone() is not None


async def _enqueue_step(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
) -> ToolResponse:
    """Enqueue an install/boot step job under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        job = await queue.enqueue(
            conn,
            kind,
            {"run_id": str(run.id)},
            job_authorizing(ctx, run.project),
            f"{run.id}:{step}",
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=tool,
                object_kind="runs",
                object_id=run.id,
                transition=step,
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
    return run_job_envelope(job, run.id)
