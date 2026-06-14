"""buildconfig.get tool tests — read_build_config called directly with DB pool + object store."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import build_configs
from kdive.mcp.tools.catalog.build_configs import read_build_config
from kdive.security.authz.context import RequestContext
from kdive.store.objectstore import ObjectStore


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def test_buildconfig_get_returns_inline_bytes_and_sha(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await seed_build_configs(conn, minio_store)

            data = KDUMP_FRAGMENT_PATH.read_bytes()

            async with pool.connection() as conn:
                resp = await read_build_config(conn, minio_store, name="kdump")

        assert resp.status == "available"
        assert resp.data["content"] == data.decode()
        assert resp.data["sha256"] == hashlib.sha256(data).hexdigest()
        assert "merge_config.sh -m" in str(resp.data["merge_recipe"])

    asyncio.run(_run())


def test_buildconfig_get_unknown_name_is_configuration_error(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    caught: list[CategorizedError] = []

    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            with pytest.raises(CategorizedError) as exc:
                await read_build_config(conn, minio_store, name="nope")
            caught.append(exc.value)

    asyncio.run(_run())
    assert caught[0].category is ErrorCategory.CONFIGURATION_ERROR


def test_buildconfig_get_tool_maps_unknown_name_to_failure_envelope(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(build_configs, "_resolve_store", lambda: minio_store)
            monkeypatch.setattr(
                build_configs,
                "current_context",
                lambda: RequestContext(
                    principal="dev-1",
                    agent_session="sess-dev",
                    projects=(),
                    roles={},
                    platform_roles=frozenset(),
                ),
            )
            app = FastMCP("build-config-test")
            build_configs.register(app, pool)
            tools = {tool.name: tool for tool in await app.list_tools()}
            result = await cast(Any, tools["buildconfig.get"]).fn("nope")

        assert isinstance(result, ToolResponse)
        assert result.object_id == "nope"
        assert result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert result.suggested_next_actions == ["buildconfig.get"]

    asyncio.run(_run())


def test_buildconfig_get_tool_maps_store_resolution_error(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = CategorizedError("store missing", category=ErrorCategory.CONFIGURATION_ERROR)

    def _raise_store() -> ObjectStore:
        raise error

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(build_configs, "_resolve_store", _raise_store)
            monkeypatch.setattr(
                build_configs,
                "current_context",
                lambda: RequestContext(
                    principal="dev-1",
                    agent_session="sess-dev",
                    projects=(),
                    roles={},
                    platform_roles=frozenset(),
                ),
            )
            app = FastMCP("build-config-test")
            build_configs.register(app, pool)
            tools = {tool.name: tool for tool in await app.list_tools()}
            result = await cast(Any, tools["buildconfig.get"]).fn("kdump")

        assert isinstance(result, ToolResponse)
        assert result.object_id == "kdump"
        assert result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert result.suggested_next_actions == ["buildconfig.get"]

    asyncio.run(_run())
