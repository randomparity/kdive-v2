"""The `jobs.*` MCP tools over the durable queue (issue #10).

Each tool is a thin FastMCP wrapper over a plain async handler that takes its
dependencies (the pool, the request context) as arguments, so handlers are tested
directly without MCP transport. A handler that raises a domain error becomes an
error :class:`~kdive.mcp.responses.ToolResponse` (with the most specific
``ErrorCategory``), never an unhandled 500.

Every read/cancel is **project-scoped** (#11): a job is visible only to a caller with
``viewer`` on the owning project (``authorizing->>'project'``), while cancellation requires
``operator``. A by-id read or cancel of a job in an ungranted project returns the same
not-found-shaped error as a missing job, so existence is not leaked (matching
``systems``/``runs``/``allocations`` getters); ``list`` returns only readable jobs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import JOBS, ObjectNotFound
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job
from kdive.domain.state import IllegalTransition, JobState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.security.context import RequestContext
from kdive.security.rbac import AuthorizationError, Role, require_role

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED})


def _error(object_id: str, category: ErrorCategory) -> ToolResponse:
    return ToolResponse(object_id=object_id, status="error", error_category=category.value)


def _in_scope(job: Job, ctx: RequestContext) -> bool:
    """True iff ``job``'s owning project is granted to ``ctx`` (#11).

    A job whose ``authorizing`` tuple carries no string ``project`` belongs to no one and
    is therefore out of scope for every caller (fail closed).
    """
    project = job.authorizing.get("project")
    return isinstance(project, str) and project in ctx.projects


def _project(job: Job) -> str:
    project = job.authorizing["project"]
    return str(project)


def _readable_projects(ctx: RequestContext) -> list[str]:
    readable: list[str] = []
    for project in ctx.projects:
        try:
            require_role(ctx, project, Role.VIEWER)
        except AuthorizationError:
            continue
        readable.append(project)
    return readable


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
        if job is None or not _in_scope(job, ctx):
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        require_role(ctx, _project(job), Role.VIEWER)
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
            if job is None or not _in_scope(job, ctx):
                return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
            require_role(ctx, _project(job), Role.VIEWER)
            if job.state in _TERMINAL or loop.time() >= deadline:
                return ToolResponse.from_job(job)
            await asyncio.sleep(POLL_INTERVAL_S)


async def cancel_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str) -> ToolResponse:
    """Transition the job to ``canceled`` (cooperative); error on a terminal job.

    Cancelling a job that has already reached a terminal state is a no-op the agent
    must be able to act on, so the error envelope carries the job's actual current
    status in ``data["current_status"]`` (the agent learns *why* without a second
    ``jobs.get``). ``error_category`` stays paired with ``status="error"``, honoring
    the envelope's "category iff failure-status" invariant — the terminal lifecycle
    state goes in ``data``, not in ``status``.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal, job_id=job_id):
        # Authorize before mutating: a job in an ungranted project must look absent and
        # never be canceled. The owning project never changes, so the read→update gap is
        # not an authz TOCTOU (the cancel still races a concurrent transition, which
        # update_state's IllegalTransition handles below).
        async with pool.connection() as conn:
            existing = await JOBS.get(conn, uid)
        if existing is None or not _in_scope(existing, ctx):
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        project = existing.authorizing["project"]
        require_role(ctx, str(project), Role.OPERATOR)
        try:
            async with pool.connection() as conn:
                job = await JOBS.update_state(conn, uid, JobState.CANCELED)
        except ObjectNotFound:
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        except IllegalTransition:
            async with pool.connection() as conn:
                current = await JOBS.get(conn, uid)
            data = {"current_status": current.state.value} if current else {}
            return ToolResponse(
                object_id=job_id,
                status="error",
                error_category=ErrorCategory.CONFIGURATION_ERROR.value,
                data=data,
            )
        return ToolResponse.from_job(job)


async def list_jobs(
    pool: AsyncConnectionPool, ctx: RequestContext, *, limit: int
) -> list[ToolResponse]:
    """Return the newest jobs (capped), each as an envelope, isolating bad rows."""
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            jobs = await queue.recent_jobs(conn, capped, _readable_projects(ctx))
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

    @app.tool(
        name="jobs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_get(
        job_id: Annotated[str, Field(description="The Job to render.")],
    ) -> ToolResponse:
        """Render a Job by ID. Requires viewer."""
        return await get_job(pool, current_context(), job_id)

    @app.tool(
        name="jobs.wait",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_wait(
        job_id: Annotated[str, Field(description="The Job to poll until terminal.")],
        timeout_s: Annotated[
            float, Field(description="Maximum seconds to wait (capped at 300).")
        ] = 30.0,
    ) -> ToolResponse:
        """Poll a Job until terminal or the timeout elapses. Requires viewer."""
        return await wait_job(pool, current_context(), job_id, timeout_s)

    @app.tool(
        name="jobs.cancel",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def jobs_cancel(
        job_id: Annotated[str, Field(description="The Job to cancel.")],
    ) -> ToolResponse:
        """Cancel a Job cooperatively; error envelope if already terminal. Requires operator."""
        return await cancel_job(pool, current_context(), job_id)

    @app.tool(
        name="jobs.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def jobs_list(
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
    ) -> list[ToolResponse]:
        """List the newest Jobs visible to the caller's readable projects. Requires viewer."""
        return await list_jobs(pool, current_context(), limit=limit)
