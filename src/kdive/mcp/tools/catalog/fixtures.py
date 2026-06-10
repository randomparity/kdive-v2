"""``fixtures.list`` — provider-organized rootfs fixture catalog entries (ADR-0089 §6).

A plain authenticated read: the fixture catalog is the provider-organized rootfs inventory,
not secret content, so there is no platform gate and no per-tool audit. It requires a valid
token (the verifier already gated the transport); the handler enforces token presence as
defence in depth. Each visible rootfs entry flattens to ``{provider, name, arch}``.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.provider_components.catalog import FixtureCatalog, load_fixture_catalog

_OBJECT_ID = "fixtures"


def _rows(catalog: FixtureCatalog) -> list[dict[str, str]]:
    """Flatten every provider's visible rootfs entries into presence rows."""
    providers = sorted({entry.provider for entry in catalog.rootfs})
    return [
        {"provider": provider, "name": entry.name, "arch": entry.arch}
        for provider in providers
        for entry in catalog.rootfs_for_provider(provider)
    ]


async def list_fixtures_tool() -> ToolResponse:
    """Return the rootfs fixture catalog entries (provider, name, arch)."""
    catalog = load_fixture_catalog()
    return ToolResponse.success(_OBJECT_ID, "ok", data={"fixtures": _rows(catalog)})


def register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
    """Register ``fixtures.list`` on ``app`` (the ``_plain`` registrar seam; pool unused)."""

    @app.tool(
        name="fixtures.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def fixtures_list() -> ToolResponse:
        """List rootfs fixture catalog entries (provider, name, arch). Requires a valid token."""
        current_context()
        return await list_fixtures_tool()
