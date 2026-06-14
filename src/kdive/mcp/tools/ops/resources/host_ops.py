"""Platform resource host operation tools (ADR-0062).

These handlers own cross-tenant host mutation and drain orchestration. Catalog reads stay
in `kdive.mcp.tools.catalog.resources`; runtime inventory register/deregister/renew tools
stay in `kdive.mcp.tools.ops.resources.registrar`.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import RESOURCES
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.tools._resource_envelopes import resource_config_error, resource_envelope
from kdive.mcp.tools.ops.breakglass import breakglass_release_allocation
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)
from kdive.services.allocation.release import ReleaseOutcome

_SET_STATUS_TOOL = "resources.set_status"
_CORDON_TOOL = "resources.cordon"
_UNCORDON_TOOL = "resources.uncordon"
_DRAIN_TOOL = "resources.drain"

# Allocations that hold a slot on the host. `requested` allocations are waiting for
# placement, not holding the host, and are not releasable (ADR-0062).
_DRAINABLE = (AllocationState.GRANTED, AllocationState.ACTIVE, AllocationState.RELEASING)
_DRAIN_MODES = ("passive", "force_release")


async def _audit_host_action(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    resource_id: UUID,
    detail: str,
) -> None:
    """Record one ``platform_audit_log`` row for an operator host action (ADR-0062)."""
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=f"resource:{resource_id}:{detail}",
                args={"resource_id": str(resource_id)},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def _denied(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.AUTHORIZATION_DENIED)


def _classify_drain_release(alloc_id: str, outcome: ReleaseOutcome) -> ToolResponse:
    """Map one break-glass release outcome to a per-allocation drain result item."""
    if outcome.released:
        return ToolResponse.success(alloc_id, "released")
    data = {"current_status": outcome.current_status} if outcome.current_status else {}
    if outcome.category is ErrorCategory.STALE_HANDLE:
        return ToolResponse.success(alloc_id, "skipped", data=data)
    return ToolResponse.failure(
        alloc_id, outcome.category or ErrorCategory.CONFIGURATION_ERROR, data=data
    )


async def set_resource_status(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str, status: str
) -> ToolResponse:
    """Set a host's health ``status`` and leave its schedulability unchanged."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=_SET_STATUS_TOOL,
            scope=f"resource:{resource_id}",
            args={"resource_id": resource_id, "status": status},
        )
        return _denied(resource_id)
    uid = _as_uuid(resource_id)
    if uid is None:
        return resource_config_error(resource_id)
    try:
        new_status = ResourceStatus(status)
    except ValueError:
        return resource_config_error(status)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
            if resource is None:
                return resource_config_error(resource_id)
            if resource.status is not new_status:
                resource = await RESOURCES.update_state(conn, uid, new_status)
            await _audit_host_action(
                conn, ctx, tool=_SET_STATUS_TOOL, resource_id=uid, detail=f"status={status}"
            )
        return resource_envelope(resource, next_actions=["resources.describe"])


async def _apply_cordon(conn: AsyncConnection, uid: UUID, *, cordoned: bool) -> Resource | None:
    """Set a host's ``cordoned`` flag and return the updated row."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE resources SET cordoned = %s WHERE id = %s RETURNING *",
            (cordoned, uid),
        )
        row = await cur.fetchone()
    return Resource.model_validate(row) if row is not None else None


async def _set_cordoned(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str, cordoned: bool, tool: str
) -> ToolResponse:
    """Toggle a host's ``cordoned`` flag; operator-only and audited."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=tool,
            scope=f"resource:{resource_id}",
            args={"resource_id": resource_id, "cordoned": cordoned},
        )
        return _denied(resource_id)
    uid = _as_uuid(resource_id)
    if uid is None:
        return resource_config_error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await _apply_cordon(conn, uid, cordoned=cordoned)
            if resource is None:
                return resource_config_error(resource_id)
            await _audit_host_action(
                conn, ctx, tool=tool, resource_id=uid, detail=f"cordoned={cordoned}"
            )
        return resource_envelope(resource, next_actions=["resources.describe"])


async def cordon_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str
) -> ToolResponse:
    """Mark a host unschedulable; placement then skips or rejects it."""
    return await _set_cordoned(pool, ctx, resource_id=resource_id, cordoned=True, tool=_CORDON_TOOL)


async def uncordon_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str
) -> ToolResponse:
    """Restore a host to schedulable; leaves ``status`` unchanged."""
    return await _set_cordoned(
        pool, ctx, resource_id=resource_id, cordoned=False, tool=_UNCORDON_TOOL
    )


def _drain_role(mode: str) -> PlatformRole:
    """`force_release` empties tenant allocations; passive cordon is operator-only."""
    if mode == "force_release":
        return PlatformRole.PLATFORM_ADMIN
    return PlatformRole.PLATFORM_OPERATOR


async def _live_allocations(conn: AsyncConnection, resource_id: UUID) -> list[Allocation]:
    """Allocations currently holding a slot on the host, oldest first."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM allocations WHERE resource_id = %s AND state = ANY(%s) "
            "ORDER BY created_at, id",
            (resource_id, [s.value for s in _DRAINABLE]),
        )
        rows = await cur.fetchall()
    return [Allocation.model_validate(row) for row in rows]


async def _force_release_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, live: list[Allocation], *, reason: str
) -> tuple[list[ToolResponse], dict[str, int]]:
    """Break-glass release each live allocation; return result items and a tally."""
    items: list[ToolResponse] = []
    for alloc in live:
        outcome = await breakglass_release_allocation(
            pool, ctx, alloc=alloc, tool=_DRAIN_TOOL, reason=reason
        )
        items.append(_classify_drain_release(str(alloc.id), outcome))
    counts = Counter(item.status for item in items)
    tally = {
        "released": counts["released"],
        "skipped": counts["skipped"],
        "failed": counts["error"],
    }
    return items, tally


async def drain_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resource_id: str,
    mode: str = "passive",
    reason: str = "",
) -> ToolResponse:
    """Cordon a host, then report or force-release its live allocations."""
    if mode not in _DRAIN_MODES:
        return resource_config_error(mode)
    try:
        require_platform_role(ctx, _drain_role(mode))
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=_DRAIN_TOOL,
            scope=f"resource:{resource_id}",
            args={"resource_id": resource_id, "mode": mode},
        )
        return _denied(resource_id)
    if mode == "force_release" and not reason.strip():
        return resource_config_error(resource_id)
    uid = _as_uuid(resource_id)
    if uid is None:
        return resource_config_error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await _apply_cordon(conn, uid, cordoned=True)
            if resource is None:
                return resource_config_error(resource_id)
            await _audit_host_action(
                conn, ctx, tool=_DRAIN_TOOL, resource_id=uid, detail="cordoned=true"
            )
        async with pool.connection() as conn:
            live = await _live_allocations(conn, uid)
        if mode == "passive":
            items = [
                ToolResponse.success(
                    str(alloc.id),
                    alloc.state.value,
                    data={"project": alloc.project, "resource_id": str(uid)},
                )
                for alloc in live
            ]
            return ToolResponse.collection(
                resource_id,
                "cordoned",
                items,
                suggested_next_actions=[_DRAIN_TOOL, "inventory.list"],
                data={"mode": mode},
            )
        release_items, tally = await _force_release_allocations(pool, ctx, live, reason=reason)
        return ToolResponse.collection(
            resource_id,
            "cordoned",
            release_items,
            suggested_next_actions=[_DRAIN_TOOL],
            data={"mode": mode, **{key: str(value) for key, value in tally.items()}},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register operator host operation tools on ``app``."""

    @app.tool(
        name="resources.set_status",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_set_status(
        resource_id: Annotated[str, Field(description="The host Resource UUID.")],
        status: Annotated[str, Field(description="Health: 'available', 'degraded', or 'offline'.")],
    ) -> ToolResponse:
        """Set a host's health status; leaves cordoned unchanged. Requires platform operator."""
        return await set_resource_status(
            pool, current_context(), resource_id=resource_id, status=status
        )

    @app.tool(
        name="resources.cordon",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_cordon(
        resource_id: Annotated[str, Field(description="The host Resource UUID to cordon.")],
    ) -> ToolResponse:
        """Mark a host unschedulable; placement skips/rejects it. Requires platform operator."""
        return await cordon_resource(pool, current_context(), resource_id=resource_id)

    @app.tool(
        name="resources.uncordon",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def resources_uncordon(
        resource_id: Annotated[str, Field(description="The host Resource UUID to uncordon.")],
    ) -> ToolResponse:
        """Restore a host to schedulable; leaves status unchanged. Requires platform operator."""
        return await uncordon_resource(pool, current_context(), resource_id=resource_id)

    @app.tool(
        name="resources.drain",
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def resources_drain(
        resource_id: Annotated[str, Field(description="The host Resource UUID to drain.")],
        mode: Annotated[
            str,
            Field(description="'passive' (operator: cordon + report) or 'force_release' (admin)."),
        ] = "passive",
        reason: Annotated[
            str,
            Field(description="Mandatory non-blank justification for 'force_release' (audited)."),
        ] = "",
    ) -> ToolResponse:
        """Cordon a host, then report or force-release its allocations."""
        return await drain_resource(
            pool, current_context(), resource_id=resource_id, mode=mode, reason=reason
        )
