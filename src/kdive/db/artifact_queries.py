"""Shared artifact lookup queries used across planes and MCP tools."""

from __future__ import annotations

from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

_RAW_VMCORE_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
_RAW_VMCORE_KEY_LIKE = "%/vmcore-%"
_REDACTED_VMCORE_LIKE = "%-redacted"


async def raw_vmcore_key(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return the System's raw ``vmcore-{method}`` object key, or ``None``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _RAW_VMCORE_KEY_SQL,
            (system_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])
