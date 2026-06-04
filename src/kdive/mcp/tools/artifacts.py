"""The `artifacts.*` MCP tools — redacted-only artifact reads (ADR-0031).

`artifacts.list(system_id)` and `artifacts.get(artifact_id)` surface **only** `redacted`
rows; a `sensitive` artifact id is shaped as not-found, so the raw vmcore is never
fetchable through the agent surface even by id. Project membership is enforced through the
owning System.
"""

from __future__ import annotations

import logging
from typing import LiteralString
from uuid import UUID

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_log = logging.getLogger(__name__)

_LIST_SQL: LiteralString = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = 'redacted' "
    "ORDER BY created_at DESC"
)
_GET_SQL: LiteralString = (
    "SELECT id, object_key, owner_id FROM artifacts "
    "WHERE id = %s AND owner_kind = 'systems' AND sensitivity = 'redacted'"
)
_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def artifacts_list(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[ToolResponse]:
    """Return the System's `redacted` artifacts as envelopes (empty list if none/absent)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            await cur.execute(_LIST_SQL, (uid,))
            rows = await cur.fetchall()
    responses: list[ToolResponse] = []
    for row in rows:
        try:
            responses.append(
                ToolResponse.success(
                    str(row["id"]),
                    "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": row["object_key"]},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", row["id"])
    return responses


async def artifacts_get(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> ToolResponse:
    """Return one `redacted` artifact's envelope, or a not-found-shaped config error.

    A missing artifact and a `sensitive` artifact are indistinguishable (both
    `configuration_error`), so the raw vmcore cannot be fetched even when its id is known.
    """
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid,))
            row = await cur.fetchone()
            if row is None:
                return _config_error(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _config_error(artifact_id)
        return ToolResponse.success(
            artifact_id,
            "available",
            suggested_next_actions=["artifacts.get"],
            refs={"object": row["object_key"]},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="artifacts.list")
    async def artifacts_list_tool(system_id: str) -> list[ToolResponse]:
        return await artifacts_list(pool, current_context(), system_id=system_id)

    @app.tool(name="artifacts.get")
    async def artifacts_get_tool(artifact_id: str) -> ToolResponse:
        return await artifacts_get(pool, current_context(), artifact_id=artifact_id)
