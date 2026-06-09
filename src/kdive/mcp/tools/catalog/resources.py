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
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext

_log = logging.getLogger(__name__)

_FLAT_CAP_KEYS = ("arch", "vcpus", "memory_mb", "concurrent_allocation_cap")


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


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register resource catalog read tools on ``app``, bound to ``pool``."""

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
