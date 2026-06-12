"""``fixtures.list`` — provider-organized rootfs baseline catalog entries (ADR-0089 §6).

A plain authenticated read: the baseline rootfs inventory is provider-organized metadata, not
secret content, so there is no platform gate and no per-tool audit. It requires a valid token
(the verifier already gated the transport); the handler enforces token presence as defence in
depth. Each baseline rootfs entry flattens to ``{provider, name, arch}``.

The rootfs catalog moved to the DB-backed ``image_catalog`` (ADR-0092); this read reports the
packaged baseline inventory (``images/seed_data/``, the metadata seeded as ``defined`` rows),
which is the same inventory it reported before the relocation. The published/registered view is
the ``images list`` operator verb, not this baseline read.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.images.seed import PACKAGED_SEED_DATA_PATH
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


def list_fixtures_tool() -> ToolResponse:
    """Return the baseline rootfs catalog entries (provider, name, arch)."""
    catalog = load_fixture_catalog(PACKAGED_SEED_DATA_PATH)
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
        return list_fixtures_tool()
