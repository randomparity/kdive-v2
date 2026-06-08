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
from kdive.domain.models import Resource, ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools.ops._auth import audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    require_platform_role,
)

_log = logging.getLogger(__name__)

_FLAT_CAP_KEYS = ("arch", "vcpus", "memory_mb", "concurrent_allocation_cap")

_SET_STATUS_TOOL = "resources.set_status"
_CORDON_TOOL = "resources.cordon"
_UNCORDON_TOOL = "resources.uncordon"


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
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE resources SET cordoned = %s WHERE id = %s RETURNING *",
                    (cordoned, uid),
                )
                row = await cur.fetchone()
            if row is None:
                return _error(resource_id)
            await _audit_host_action(
                conn, ctx, tool=tool, resource_id=uid, detail=f"cordoned={cordoned}"
            )
        return _resource_envelope(Resource.model_validate(row), next_actions=["resources.describe"])


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
