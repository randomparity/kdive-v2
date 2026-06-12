"""buildconfig.get tool tests — read_build_config called directly with DB pool + object store."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.catalog.build_configs import read_build_config
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
