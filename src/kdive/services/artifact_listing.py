"""Authorized artifact listing queries shared by MCP tool surfaces."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.log import bind_context
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

_LIST_REDACTED_SYSTEM_SQL = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = 'redacted' "
    "ORDER BY created_at DESC"
)
_SYSTEM_PROJECT_SQL = "SELECT project FROM systems WHERE id = %s"


class RedactedArtifact(NamedTuple):
    id: str
    object_key: str


async def list_redacted_system_artifacts(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[RedactedArtifact]:
    """Return redacted artifact rows for an authorized System; absent systems return empty."""
    try:
        uid = UUID(system_id)
    except ValueError:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SYSTEM_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            require_role(ctx, owner["project"], Role.VIEWER)
            await cur.execute(_LIST_REDACTED_SYSTEM_SQL, (uid,))
            rows = await cur.fetchall()
    return [RedactedArtifact(id=str(row["id"]), object_key=str(row["object_key"])) for row in rows]
