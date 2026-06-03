"""The `jobs.*` MCP tools over the durable queue (issue #10).

Each tool is a thin FastMCP wrapper over a plain async handler that takes its
dependencies (the pool, the request context) as arguments, so handlers are tested
directly without MCP transport. A handler that raises a domain error becomes an
error :class:`~kdive.mcp.responses.ToolResponse` (with the most specific
``ErrorCategory``), never an unhandled 500. M0 does not scope these by
principal/project — see the issue #10 design's isolation posture; #11 (RBAC) adds
scoping.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS, ObjectNotFound
from kdive.domain.errors import ErrorCategory
from kdive.domain.state import IllegalTransition, JobState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})


def _error(object_id: str, category: ErrorCategory) -> ToolResponse:
    return ToolResponse(object_id=object_id, status="error", error_category=category.value)


def _as_uuid(job_id: str) -> UUID | None:
    try:
        return UUID(job_id)
    except ValueError:
        return None


async def get_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str) -> ToolResponse:
    """Return the job's handle envelope, or an error envelope if absent/malformed.

    Binds the request's ``principal`` + ``job_id`` into the structured-log context
    (ADR-0014) so every record emitted while serving this read is attributed, whether
    the handler is reached through the MCP tool or called directly.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal, job_id=job_id):
        async with pool.connection() as conn:
            job = await JOBS.get(conn, uid)
        if job is None:
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        return ToolResponse.from_job(job)


async def wait_job(
    pool: AsyncConnectionPool, ctx: RequestContext, job_id: str, timeout_s: float
) -> ToolResponse:
    """Poll until the job is terminal or ``timeout_s`` (clamped) elapses.

    Each poll acquires and releases a pool connection (holds none while sleeping). A
    non-positive timeout means a single read.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    with bind_context(principal=ctx.principal, job_id=job_id):
        while True:
            async with pool.connection() as conn:
                job = await JOBS.get(conn, uid)
            if job is None:
                return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
            if job.state in _TERMINAL or loop.time() >= deadline:
                return ToolResponse.from_job(job)
            await asyncio.sleep(POLL_INTERVAL_S)


async def cancel_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str) -> ToolResponse:
    """Transition the job to ``canceled`` (cooperative); error on a terminal job."""
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal, job_id=job_id):
        try:
            async with pool.connection() as conn:
                job = await JOBS.update_state(conn, uid, JobState.CANCELED)
        except (ObjectNotFound, IllegalTransition):
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        return ToolResponse.from_job(job)


async def list_jobs(
    pool: AsyncConnectionPool, ctx: RequestContext, *, limit: int
) -> list[ToolResponse]:
    """Return the newest jobs (capped), each as an envelope, isolating bad rows."""
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            jobs = await queue.recent_jobs(conn, capped)
        responses: list[ToolResponse] = []
        for job in jobs:
            try:
                responses.append(ToolResponse.from_job(job))
            except ValueError:
                _log.warning("job %s violates the response invariant; degraded", job.id)
                responses.append(_error(str(job.id), ErrorCategory.INFRASTRUCTURE_FAILURE))
        return responses


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the four `jobs.*` tools on ``app``, bound to ``pool``.

    Each wrapper resolves the request context (raising before the handler runs if no
    verified token reached the tool) and delegates; the handler owns its log context.
    """

    @app.tool(name="jobs.get")
    async def jobs_get(job_id: str) -> ToolResponse:
        return await get_job(pool, current_context(), job_id)

    @app.tool(name="jobs.wait")
    async def jobs_wait(job_id: str, timeout_s: float = 30.0) -> ToolResponse:
        return await wait_job(pool, current_context(), job_id, timeout_s)

    @app.tool(name="jobs.cancel")
    async def jobs_cancel(job_id: str) -> ToolResponse:
        return await cancel_job(pool, current_context(), job_id)

    @app.tool(name="jobs.list")
    async def jobs_list(limit: int = DEFAULT_LIST_LIMIT) -> list[ToolResponse]:
        return await list_jobs(pool, current_context(), limit=limit)
