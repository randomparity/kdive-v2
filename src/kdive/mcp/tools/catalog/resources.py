"""The `resources.*` MCP tools (Discovery plane reads) (ADR-0023).

Thin FastMCP wrappers over plain async handlers that take the pool + request context as
arguments (tested directly, never through MCP). Resources are shared infrastructure (no
`project` column), so reads require only an authenticated context — no RBAC scoping. The
nested `capabilities` jsonb is projected to a flat `dict[str, str]` for the response
envelope (ADR-0019 `data` is `dict[str, str]`).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import RESOURCES
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools.ops._auth import audit_platform_denial, held_platform_roles
from kdive.mcp.tools.ops.breakglass import breakglass_release_allocation
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)
from kdive.services.allocation_release import ReleaseOutcome

_log = logging.getLogger(__name__)

_FLAT_CAP_KEYS = ("arch", "vcpus", "memory_mb", "concurrent_allocation_cap")

_SET_STATUS_TOOL = "resources.set_status"
_CORDON_TOOL = "resources.cordon"
_UNCORDON_TOOL = "resources.uncordon"
_DRAIN_TOOL = "resources.drain"

# Allocations that hold a slot on the host — the releasable states. Deliberately not the
# broader capacity set (which also counts `requested`): a `requested` allocation is waiting
# for placement, not holding the host, and is not releasable (ADR-0062 §3).
_DRAINABLE = (AllocationState.GRANTED, AllocationState.ACTIVE, AllocationState.RELEASING)
_DRAIN_MODES = ("passive", "force_release")


def _error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _resource_capability_data(resource: Resource) -> dict[str, str]:
    """Flatten the capabilities jsonb to string values for the envelope."""
    caps = resource.capabilities
    data: dict[str, str] = {"kind": resource.kind.value}
    for key in _FLAT_CAP_KEYS:
        if key in caps:
            data[key] = str(caps[key])
    transports = caps.get("transports")
    if isinstance(transports, (list, tuple)):
        data["transports"] = ",".join(str(t) for t in transports)
    return data


def _resource_envelope(resource: Resource, *, next_actions: list[str]) -> ToolResponse:
    return ToolResponse.success(
        str(resource.id),
        resource.status.value,
        suggested_next_actions=next_actions,
        data=_resource_capability_data(resource),
    )


async def _fetch_resource_rows(
    conn: AsyncConnection, kind: ResourceKind | None
) -> list[dict[str, Any]]:
    if kind is None:
        query = "SELECT * FROM resources ORDER BY created_at, id"
        params: tuple[object, ...] = ()
    else:
        query = "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id"
        params = (kind.value,)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _resource_row_error(row: dict[str, Any]) -> ToolResponse:
    object_id = row.get("id")
    return ToolResponse.failure(
        str(object_id) if object_id is not None else "resources.list",
        ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


async def list_resources_tool(
    pool: AsyncConnectionPool, ctx: RequestContext, *, kind: str | None
) -> ToolResponse:
    """Return every resource (optionally filtered by ``kind``) in one collection envelope."""
    if kind is None:
        resource_kind = None
    else:
        try:
            resource_kind = ResourceKind(kind)
        except ValueError:
            return ToolResponse.failure("resources.list", ErrorCategory.CONFIGURATION_ERROR)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            rows = await _fetch_resource_rows(conn, resource_kind)
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                resource = Resource.model_validate(row)
                responses.append(
                    _resource_envelope(
                        resource, next_actions=["resources.describe", "allocations.request"]
                    )
                )
            except ValueError:
                _log.warning(
                    "resource %s violates the response invariant; degraded",
                    row.get("id", "<missing>"),
                    exc_info=True,
                )
                responses.append(_resource_row_error(row))
        return ToolResponse.collection(
            "resources",
            "ok",
            responses,
            suggested_next_actions=["resources.describe", "allocations.request"],
        )


async def describe_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, resource_id: str
) -> ToolResponse:
    """Return one resource's envelope with pool/cost_class/host_uri, or an error."""
    try:
        uid = UUID(resource_id)
    except ValueError:
        return _error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
        if resource is None:
            return _error(resource_id)
        envelope = _resource_envelope(resource, next_actions=["allocations.request"])
        envelope.data["pool"] = resource.pool
        envelope.data["cost_class"] = resource.cost_class
        envelope.data["host_uri"] = resource.host_uri
        return envelope


async def _audit_host_action(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    resource_id: UUID,
    detail: str,
) -> None:
    """Record one ``platform_audit_log`` row for an operator host action (ADR-0062 §3).

    Host actions are cross-tenant (a `resources` row has no `project`), so they use the
    guard-exempt platform writer. ``scope`` carries the host id and the applied change so
    the trail is self-describing without an object column.
    """
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
            ),
        )


def _denied(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.AUTHORIZATION_DENIED)


def _classify_drain_release(alloc_id: str, outcome: ReleaseOutcome) -> ToolResponse:
    """Map one break-glass release outcome to a per-allocation drain result item (ADR-0062 §3).

    ``released`` → success; ``STALE_HANDLE`` (already terminal — nothing to do) → ``skipped``;
    any other category → failure. ``current_status`` is carried only when present — the
    reconcile-failure outcome has none — mirroring the sibling break-glass envelope so the two
    tools' result shapes never diverge.
    """
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
    """Set a host's health ``status`` (``available``/``degraded``/``offline``).

    Operator-only (``platform_operator``). Touches only the health axis — the ``cordoned``
    schedulability flag is left unchanged (ADR-0062 §3). A re-set to the current status is a
    success no-op. Requires a known host id and a valid status value.
    """
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
        return _error(resource_id)
    try:
        new_status = ResourceStatus(status)
    except ValueError:
        return _error(status)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await RESOURCES.get(conn, uid)
            if resource is None:
                return _error(resource_id)
            if resource.status is not new_status:
                resource = await RESOURCES.update_state(conn, uid, new_status)
            await _audit_host_action(
                conn, ctx, tool=_SET_STATUS_TOOL, resource_id=uid, detail=f"status={status}"
            )
        return _resource_envelope(resource, next_actions=["resources.describe"])


async def _apply_cordon(conn: AsyncConnection, uid: UUID, *, cordoned: bool) -> Resource | None:
    """Set a host's ``cordoned`` flag and return the updated row, or ``None`` if no such host.

    The pure UPDATE shared by ``resources.cordon``/``uncordon`` and ``resources.drain``; it
    carries no role check or audit so each caller applies its own authorization and audit tool.
    """
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
    """Toggle a host's ``cordoned`` flag; operator-only, audited. Independent of ``status``."""
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
        return _error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await _apply_cordon(conn, uid, cordoned=cordoned)
            if resource is None:
                return _error(resource_id)
            await _audit_host_action(
                conn, ctx, tool=tool, resource_id=uid, detail=f"cordoned={cordoned}"
            )
        return _resource_envelope(resource, next_actions=["resources.describe"])


async def cordon_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str
) -> ToolResponse:
    """Mark a host unschedulable (``cordoned := true``); placement then skips/rejects it."""
    return await _set_cordoned(pool, ctx, resource_id=resource_id, cordoned=True, tool=_CORDON_TOOL)


async def uncordon_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str
) -> ToolResponse:
    """Restore a host to schedulable (``cordoned := false``); leaves ``status`` unchanged."""
    return await _set_cordoned(
        pool, ctx, resource_id=resource_id, cordoned=False, tool=_UNCORDON_TOOL
    )


def _drain_role(mode: str) -> PlatformRole:
    """`force_release` empties every tenant's allocations → admin; passive cordon → operator."""
    if mode == "force_release":
        return PlatformRole.PLATFORM_ADMIN
    return PlatformRole.PLATFORM_OPERATOR


async def _live_allocations(conn: AsyncConnection, resource_id: UUID) -> list[Allocation]:
    """Allocations currently holding a slot on the host (``_DRAINABLE``), oldest first."""
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
    """Break-glass release each live allocation; return per-allocation items + a tally.

    A per-allocation failure does not stop the drain or roll back earlier releases — the host
    is left partially drained and still cordoned, so the action is re-invokable over the
    remaining set (ADR-0062 §3).
    """
    items: list[ToolResponse] = []
    tally = {"released": 0, "skipped": 0, "failed": 0}
    for alloc in live:
        outcome = await breakglass_release_allocation(
            pool, ctx, alloc=alloc, tool=_DRAIN_TOOL, reason=reason
        )
        item = _classify_drain_release(str(alloc.id), outcome)
        items.append(item)
        if item.status == "released":
            tally["released"] += 1
        elif item.status == "skipped":
            tally["skipped"] += 1
        else:
            tally["failed"] += 1
    return items, tally


async def drain_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resource_id: str,
    mode: str = "passive",
    reason: str = "",
) -> ToolResponse:
    """Cordon a host, then report (``passive``) or force-release (``force_release``) allocations.

    ``passive`` (``platform_operator``) cordons and returns the host's live allocations to finish
    or expire. ``force_release`` (``platform_admin`` + a non-blank ``reason``) cordons and routes
    each live allocation through the same break-glass attribution path as ``ops.force_release``,
    returning a per-allocation result list (released/skipped/failed). Both modes leave the host
    ``cordoned``; the action carries no persisted state (ADR-0062 §3).
    """
    if mode not in _DRAIN_MODES:
        return _error(mode)
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
        return _error(resource_id)
    uid = _as_uuid(resource_id)
    if uid is None:
        return _error(resource_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resource = await _apply_cordon(conn, uid, cordoned=True)
            if resource is None:
                return _error(resource_id)
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
        items, tally = await _force_release_allocations(pool, ctx, live, reason=reason)
        return ToolResponse.collection(
            resource_id,
            "cordoned",
            items,
            suggested_next_actions=[_DRAIN_TOOL],
            data={"mode": mode, **{key: str(value) for key, value in tally.items()}},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `resources.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="resources.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_list(
        kind: Annotated[
            str | None,
            Field(description="Filter by resource kind (e.g. 'local-libvirt'); omit for all."),
        ] = None,
    ) -> ToolResponse:
        """List Resources, optional kind. Requires a valid token; no project membership needed."""
        return await list_resources_tool(pool, current_context(), kind=kind)

    @app.tool(
        name="resources.describe",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_describe(
        resource_id: Annotated[str, Field(description="The Resource UUID to describe.")],
    ) -> ToolResponse:
        """Describe a Resource. Requires a valid token; no project membership needed."""
        return await describe_resource(pool, current_context(), resource_id)

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
        """Cordon a host, then report (passive) or force-release (force_release) its allocations.

        passive requires platform operator; force_release requires platform admin + a reason.
        """
        return await drain_resource(
            pool, current_context(), resource_id=resource_id, mode=mode, reason=reason
        )
