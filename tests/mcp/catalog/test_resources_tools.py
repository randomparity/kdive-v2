"""resources.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import resources as resources_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _discovery(cap: int = 2) -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )


async def _register(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, _discovery(), pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


def test_list_returns_host_with_flat_capability_projection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind=None)
        assert len(responses) == 1
        resp = responses[0]
        assert resp.object_id == res_id
        assert resp.status == "available"
        assert resp.data["kind"] == "local-libvirt"
        assert resp.data["arch"] == "x86_64"
        assert resp.data["vcpus"] == "8"
        assert resp.data["memory_mb"] == "16384"
        assert resp.data["transports"] == "gdbstub"
        assert resp.data["concurrent_allocation_cap"] == "2"

    asyncio.run(_run())


def test_list_kind_filter_miss_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind="nope")
        assert len(responses) == 1
        assert responses[0].status == "error"
        assert responses[0].error_category == "configuration_error"

    asyncio.run(_run())


def test_list_malformed_resource_row_degrades_to_infrastructure_failure(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb")
            responses = await resources_tools.list_resources_tool(pool, CTX, kind="local-libvirt")
        assert len(responses) == 1
        assert responses[0].object_id == res_id
        assert responses[0].status == "error"
        assert responses[0].error_category == "infrastructure_failure"

    asyncio.run(_run())


def test_describe_adds_pool_cost_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.describe_resource(pool, CTX, res_id)
        assert resp.status == "available"
        assert resp.data["pool"] == "local-libvirt"
        assert resp.data["cost_class"] == "local"
        assert resp.data["host_uri"] == "qemu:///system"

    asyncio.run(_run())


def test_describe_unknown_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_describe_malformed_id_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
