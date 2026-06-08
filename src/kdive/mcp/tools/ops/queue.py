"""Queue-control `ops.*` MCP tools (#138, ADR-0062).

``ops.queue_pause`` / ``ops.queue_resume`` toggle the single-row ``ops_control``
``queue_paused`` flag the worker reads before each ``dequeue``; pausing freezes the
worker's claim loop only (the reconciler keeps enqueuing, and those jobs wait for
resume — a processing freeze, not a control-plane freeze). ``ops.jobs_list`` is the
platform view of ``jobs.list``: a read-only, cross-project queue-depth / per-job-state
inspection. All three gate on ``require_platform_role(PLATFORM_OPERATOR)`` and audit to
``platform_audit_log``; the handlers take an injected pool + context so they are tested
directly without MCP transport.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops._auth import audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_QUEUE_OBJECT_ID = "queue"
_JOBS_OBJECT_ID = "jobs"
_PAUSE_TOOL = "ops.queue_pause"
_RESUME_TOOL = "ops.queue_resume"
_JOBS_LIST_TOOL = "ops.jobs_list"
_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


async def queue_pause(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """Pause the worker's claim loop; ``platform_operator``, audited (ADR-0062)."""
    return await _set_paused(pool, ctx, paused=True, tool=_PAUSE_TOOL)


async def queue_resume(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """Resume the worker's claim loop; ``platform_operator``, audited (ADR-0062)."""
    return await _set_paused(pool, ctx, paused=False, tool=_RESUME_TOOL)


async def _set_paused(
    pool: AsyncConnectionPool, ctx: RequestContext, *, paused: bool, tool: str
) -> ToolResponse:
    """Gate ``platform_operator``, toggle ``queue_paused``, audit (denials too)."""
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(pool, ctx, tool=tool, scope="queue")
            return ToolResponse.failure(
                _QUEUE_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[tool],
            )
        # One transaction: the flag flip and its audit row commit together or neither
        # does, so a failed audit write can never leave a paused/resumed queue unaudited
        # (the house pattern, accounting.set_budget). `set_queue_paused`'s own
        # `conn.transaction()` nests as a savepoint here; the outer block owns the commit.
        async with pool.connection() as conn, conn.transaction():
            await queue.set_queue_paused(conn, paused)
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=tool,
                    scope="queue",
                    args={"queue_paused": paused},
                    platform_role=held_platform_roles(ctx),
                ),
            )
        return ToolResponse.success(
            _QUEUE_OBJECT_ID,
            "paused" if paused else "running",
            suggested_next_actions=[_JOBS_LIST_TOOL],
            data={"queue_paused": "true" if paused else "false"},
        )


async def jobs_list(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    states: list[str] | None = None,
    limit: int = _DEFAULT_LIST_LIMIT,
) -> ToolResponse:
    """Cross-project queue depth + per-job state; ``platform_operator``, read-audited.

    The platform view of ``jobs.list`` (ADR-0062): unlike the per-project ``jobs.list``
    this spans **every** project, so it gates on ``platform_operator`` rather than a
    per-project role. ``states`` optionally filters the per-job rows to the given job
    states (the depth summary always covers all states). A successful read is audited; a
    denial is audited only when the caller holds ≥1 platform role (else the routine
    no-grant case, unrecorded to avoid write-amplification — ADR-0043 §4).
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(pool, ctx, tool=_JOBS_LIST_TOOL, scope="queue")
            return ToolResponse.failure(
                _JOBS_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_JOBS_LIST_TOOL],
            )
        try:
            parsed_states = _parse_states(states)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _JOBS_OBJECT_ID,
                exc.category,
                suggested_next_actions=[_JOBS_LIST_TOOL],
            )
        capped = max(1, min(limit, _MAX_LIST_LIMIT))
        async with pool.connection() as conn:
            depth = await queue.queue_depth(conn)
            jobs = await queue.all_recent_jobs(conn, capped, states=parsed_states)
            async with conn.transaction():
                await audit.record_platform(
                    conn,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    event=audit.PlatformAuditEvent(
                        tool=_JOBS_LIST_TOOL,
                        scope="all-projects",
                        args={
                            "states": None
                            if parsed_states is None
                            else [state.value for state in parsed_states],
                            "limit": capped,
                        },
                        platform_role=held_platform_roles(ctx),
                    ),
                )
        return _jobs_response(depth, jobs)


def _parse_states(states: list[str] | None) -> list[JobState] | None:
    """Return the validated state list, or ``None`` for all states.

    An unknown state string is a caller error (``configuration_error``) rather than a
    silently-empty result. An empty list is kept as-is (it yields no per-job rows).

    Raises:
        CategorizedError: ``states`` contains an unknown job state.
    """
    if states is None:
        return None
    parsed: list[JobState] = []
    for state in states:
        try:
            parsed.append(JobState(state))
        except ValueError:
            raise CategorizedError(
                f"unknown job state {state!r}; expected one of {sorted(s.value for s in JobState)}",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None
    return parsed


def _jobs_response(depth: dict[str, int], jobs: list[Job]) -> ToolResponse:
    items = [ToolResponse.success(str(job.id), job.state.value, data=_job_row(job)) for job in jobs]
    return ToolResponse.collection(
        _JOBS_OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=[_PAUSE_TOOL, _RESUME_TOOL],
        data={f"depth_{state}": str(count) for state, count in sorted(depth.items())},
    )


def _job_row(job: Job) -> dict[str, str]:
    """One cross-project job summary for the platform view (no payload — untrusted)."""
    return {
        "kind": job.kind.value,
        "state": job.state.value,
        "project": job.authorizing["project"],
        "attempt": str(job.attempt),
        "worker_id": job.worker_id or "",
    }


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the queue-control `ops.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_PAUSE_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_queue_pause() -> ToolResponse:
        """Pause the worker's claim loop (jobs in flight finish). Requires platform operator."""
        return await queue_pause(pool, current_context())

    @app.tool(
        name=_RESUME_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_queue_resume() -> ToolResponse:
        """Resume the worker's claim loop. Requires platform operator."""
        return await queue_resume(pool, current_context())

    @app.tool(
        name=_JOBS_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def ops_jobs_list(
        states: Annotated[
            list[str] | None,
            Field(description="Filter per-job rows to these job states; omit for all."),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum per-job rows returned (capped at 200).")
        ] = _DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """Cross-project queue depth and per-job state. Requires platform operator."""
        return await jobs_list(pool, current_context(), states=states, limit=limit)
