"""The `allocations.*` MCP tools — the Allocation admission/lifecycle surface (ADR-0023).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`request` admits against the per-host cap (core `admit`); `release` drives a granted/active
allocation to `released` under a per-allocation advisory lock with an `IllegalTransition`
backstop; `get`/`list` render an allocation through `_envelope_for_allocation`, which maps
the terminal `failed` state to a `failure` envelope (its value collides with the response
envelope's failure-status set). RBAC: `request`/`release` require `operator`; reads require
project membership. Authz denials raise (ADR-0020: no authz `ErrorCategory`).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.allocation_admission import admit
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
_DEFAULT_KIND = "local-libvirt"
_RELEASABLE = (AllocationState.GRANTED, AllocationState.ACTIVE)


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_allocation(alloc: Allocation) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6)."""
    if alloc.state is AllocationState.FAILED:
        return ToolResponse.failure(
            str(alloc.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": alloc.state.value},
        )
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data={"project": alloc.project},
    )


async def _resolve_resource(
    conn: AsyncConnection, resource_id: UUID | None, kind: str
) -> Resource | None:
    if resource_id is not None:
        return await RESOURCES.get(conn, resource_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id LIMIT 1", (kind,)
        )
        row = await cur.fetchone()
    return Resource.model_validate(row) if row else None


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    resource_id: str | None = None,
    kind: str | None = None,
) -> ToolResponse:
    """Admit an allocation against the selected host's per-host cap."""
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        resolved_id = _as_uuid(resource_id) if resource_id is not None else None
        if resource_id is not None and resolved_id is None:
            return _config_error(resource_id)
        async with pool.connection() as conn:
            resource = await _resolve_resource(conn, resolved_id, kind or _DEFAULT_KIND)
            if resource is None:
                return _config_error(resource_id or (kind or _DEFAULT_KIND))
            try:
                outcome = await admit(conn, ctx, resource=resource, project=project)
            except CategorizedError as exc:
                return ToolResponse.failure(str(resource.id), exc.category)
        if outcome.granted and outcome.allocation is not None:
            return ToolResponse.success(
                str(outcome.allocation.id),
                "granted",
                suggested_next_actions=["allocations.get", "allocations.release"],
                data={"resource_id": str(resource.id), "project": project},
            )
        _log.info("allocation denied for project %s on resource %s (at cap)", project, resource.id)
        return ToolResponse.failure(
            str(resource.id),
            ErrorCategory.ALLOCATION_DENIED,
            suggested_next_actions=["allocations.list"],
            data={
                "reason": outcome.reason or "at_capacity",
                "cap": str(outcome.cap),
                "in_use": str(outcome.in_use),
            },
        )


async def get_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Return an allocation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
        # A row in an ungranted project is indistinguishable from not-found (no leak).
        if alloc is None or alloc.project not in ctx.projects:
            return _config_error(allocation_id)
        return _envelope_for_allocation(alloc)


async def _transition_and_audit(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc_id: UUID,
    frm: AllocationState,
    to: AllocationState,
    *,
    project: str,
) -> None:
    await ALLOCATIONS.update_state(conn, alloc_id, to)
    await audit.record(
        conn,
        ctx,
        tool="allocations.release",
        object_kind="allocations",
        object_id=alloc_id,
        transition=f"{frm.value}->{to.value}",
        args={"allocation_id": str(alloc_id)},
        project=project,
    )


async def release_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Drive an allocation to ``released`` (under a per-allocation lock)."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _config_error(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            try:
                return await _release_locked(conn, ctx, uid, project=alloc.project)
            except IllegalTransition:
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # provision path). Caught OUTSIDE the rolled-back transaction; re-read.
                async with pool.connection() as conn2:
                    latest = await ALLOCATIONS.get(conn2, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return ToolResponse.failure(
                    allocation_id, ErrorCategory.CONFIGURATION_ERROR, data=data
                )


async def _release_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    """Read the state under the per-allocation lock and drive it to ``released``."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.ALLOCATION, uid):
        current = await ALLOCATIONS.get(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state not in (*_RELEASABLE, AllocationState.RELEASING):
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.CONFIGURATION_ERROR,
                data={"current_status": current.state.value},
            )
        if current.state in _RELEASABLE:
            await _transition_and_audit(
                conn, ctx, uid, current.state, AllocationState.RELEASING, project=project
            )
        await _transition_and_audit(
            conn, ctx, uid, AllocationState.RELEASING, AllocationState.RELEASED, project=project
        )
    return ToolResponse.success(str(uid), "released")


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> list[ToolResponse]:
    """Return the newest allocations for ``project``, each as an envelope."""
    require_project(ctx, project)
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM allocations WHERE project = %s "
                "ORDER BY created_at DESC, id LIMIT %s",
                (project, capped),
            )
            rows = await cur.fetchall()
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                responses.append(_envelope_for_allocation(Allocation.model_validate(row)))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(
                        str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE
                    )
                )
        return responses


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="allocations.request")
    async def allocations_request(
        project: str, resource_id: str | None = None, kind: str | None = None
    ) -> ToolResponse:
        return await request_allocation(
            pool, current_context(), project=project, resource_id=resource_id, kind=kind
        )

    @app.tool(name="allocations.get")
    async def allocations_get(allocation_id: str) -> ToolResponse:
        return await get_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.release")
    async def allocations_release(allocation_id: str) -> ToolResponse:
        return await release_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.list")
    async def allocations_list(project: str, limit: int = DEFAULT_LIST_LIMIT) -> list[ToolResponse]:
        return await list_allocations(pool, current_context(), project=project, limit=limit)
