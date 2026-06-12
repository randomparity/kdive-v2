"""``buildconfig.get`` read tool: the canonical kdump fragment served inline (ADR-0096).

An agent building a kernel from source can call this tool to retrieve the seeded kdump
fragment, its sha256, and a merge recipe.  Because the fragment is non-sensitive (it
contains only kernel-config options, no secrets), the raw bytes are returned inline.

The tool is read-only and requires only a verified token — no project-scope RBAC is
needed for a shared, operator-seeded catalog resource (the ``images.list`` / ``shapes.list``
precedent: shared-infra reads need only an authenticated caller).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.build_configs.catalog import get_build_config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.store.objectstore import ObjectStore

_TOOL = "buildconfig.get"

_MERGE_RECIPE = (
    "make defconfig && scripts/kconfig/merge_config.sh -m .config kdump.config "
    "&& make olddefconfig  # then verify every CONFIG_* in kdump.config is present in .config"
)


async def read_build_config(
    conn: AsyncConnection,
    store: ObjectStore,
    *,
    name: str,
) -> ToolResponse:
    """Fetch the seeded build-config fragment by name, returning bytes, sha256, and merge recipe.

    Args:
        conn: An open async psycopg connection.
        store: The object store that holds the fragment bytes.
        name: The fragment name to retrieve (e.g. ``"kdump"``).

    Returns:
        A :class:`ToolResponse` carrying ``content`` (the raw fragment text),
        ``sha256`` (the catalog digest), and ``merge_recipe`` (the ``merge_config.sh``
        invocation to apply the fragment onto a defconfig).

    Raises:
        CategorizedError: CONFIGURATION_ERROR when ``name`` is unknown.
    """
    entry = await get_build_config(conn, name)
    if entry is None:
        raise CategorizedError(
            f"build-config fragment {name!r} not found in the catalog",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    fetched = await asyncio.to_thread(store.get_artifact, entry.object_key, None)
    data = fetched.data
    entry.verify_bytes(data)
    return ToolResponse.success(
        name,
        "available",
        data={
            "content": data.decode(),
            "sha256": entry.sha256,
            "merge_recipe": _MERGE_RECIPE,
        },
    )


def _resolve_store() -> ObjectStore:
    """Resolve the object store from env; deferred so registration never fails without S3."""
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env()


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``buildconfig.get`` read tool on ``app``, bound to ``pool``."""
    _store: ObjectStore | None = None

    @app.tool(
        name=_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def buildconfig_get_tool(
        name: Annotated[
            str,
            Field(description="The build-config fragment name to retrieve (e.g. kdump)."),
        ],
    ) -> ToolResponse:
        """Fetch a seeded kernel-config fragment inline with sha256 and merge recipe. Auth only."""
        nonlocal _store
        ctx = current_context()
        _ = ctx  # authenticated caller established; no project RBAC for shared catalog
        if _store is None:
            _store = _resolve_store()
        async with pool.connection() as conn:
            return await read_build_config(conn, _store, name=name)
