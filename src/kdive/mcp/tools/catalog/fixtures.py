"""``fixtures.list`` — provider-organized rootfs baseline catalog entries (ADR-0089 §6).

A plain authenticated read: the baseline rootfs inventory is provider-organized metadata, not
secret content, so there is no platform gate and no per-tool audit. It requires a valid token
(the verifier already gated the transport); the handler enforces token presence as defence in
depth. Each baseline rootfs entry flattens to ``{provider, name, arch}``.

The baseline rootfs catalog now lives only in the DB-backed ``image_catalog`` (ADR-0112): image
definitions were removed from code (the packaged ``seed_data`` YAML) and load from
``systems.toml`` via the inventory reconcile. This read reports the public catalog rows — the
same provider-organized inventory it reported before, now sourced from the reconciled DB instead
of packaged YAML. The published/registered detail view is the ``images list`` operator verb.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ImageVisibility
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta

_OBJECT_ID = "fixtures"


async def _public_rows(pool: AsyncConnectionPool) -> list[JsonValue]:
    """Read the public catalog rows, flattened to ``{provider, name, arch}`` presence rows.

    Ordered by ``(provider, name, arch)`` so the listing is deterministic across passes.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT provider, name, arch FROM image_catalog "
            "WHERE visibility = %s AND owner IS NULL "
            "ORDER BY provider, name, arch",
            (ImageVisibility.PUBLIC.value,),
        )
        rows = await cur.fetchall()
    return [{"provider": row["provider"], "name": row["name"], "arch": row["arch"]} for row in rows]


async def list_fixtures_tool(pool: AsyncConnectionPool) -> ToolResponse:
    """Return the public baseline catalog entries (provider, name, arch) from the DB."""
    return ToolResponse.success(_OBJECT_ID, "ok", data={"fixtures": await _public_rows(pool)})


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register ``fixtures.list`` on ``app``."""

    @app.tool(
        name="fixtures.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_list() -> ToolResponse:
        """List rootfs fixture catalog entries (provider, name, arch). Requires a valid token."""
        current_context()
        return await list_fixtures_tool(pool)
