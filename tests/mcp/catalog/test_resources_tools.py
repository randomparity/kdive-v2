"""resources.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import resources as resources_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import PlatformRole
from kdive.services.resource_discovery import register_discovered_resource
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
        res = await register_discovered_resource(
            conn, _discovery().list_resources()[0], pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


def test_list_returns_host_with_flat_capability_projection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind=None)
        assert responses.object_id == "resources"
        assert responses.status == "ok"
        items = responses.collection_items()
        assert len(items) == 1
        resp = items[0]
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
        assert responses.status == "error"
        assert responses.error_category == "configuration_error"

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
        items = responses.collection_items()
        assert len(items) == 1
        assert items[0].object_id == res_id
        assert items[0].status == "error"
        assert items[0].error_category == "infrastructure_failure"

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


_OPERATOR = RequestContext(
    principal="op-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
)
_NON_OPERATOR = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
_AUDITOR = RequestContext(
    principal="auditor-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
)


async def _row(pool: AsyncConnectionPool, res_id: str) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT status, cordoned FROM resources WHERE id = %s", (UUID(res_id),))
        fetched = await cur.fetchone()
    assert fetched is not None
    status, cordoned = fetched
    return {"status": status, "cordoned": cordoned}


async def _platform_audit_count(pool: AsyncConnectionPool, tool: str) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log WHERE tool = %s", (tool,))
        fetched = await cur.fetchone()
    assert fetched is not None
    return int(fetched[0])


async def _platform_audit_rows(pool: AsyncConnectionPool) -> list[tuple[object, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope FROM platform_audit_log ORDER BY ts"
        )
        return list(await cur.fetchall())


def test_set_status_changes_health_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_count(pool, "resources.set_status")
        assert resp.status == "degraded"
        assert row == {"status": "degraded", "cordoned": False}
        assert audited == 1

    asyncio.run(_run())


def test_set_status_same_value_is_noop_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="available"
            )
            row = await _row(pool, res_id)
        assert resp.status == "available"
        assert row["status"] == "available"

    asyncio.run(_run())


def test_set_status_invalid_value_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="nope"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=str(uuid4()), status="offline"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_does_not_clear_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        # set_status offline must not clear an operator's cordon (orthogonal axes).
        assert row == {"status": "offline", "cordoned": True}

    asyncio.run(_run())


def test_cordon_then_uncordon_toggles_only_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            # Make the host degraded first; cordon/uncordon must not touch status.
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            cordoned = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_cordon = await _row(pool, res_id)
            await resources_tools.uncordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_uncordon = await _row(pool, res_id)
            cordon_audited = await _platform_audit_count(pool, "resources.cordon")
            uncordon_audited = await _platform_audit_count(pool, "resources.uncordon")
        assert cordoned.status == "degraded"
        assert after_cordon == {"status": "degraded", "cordoned": True}
        # uncordon does not change status: still degraded.
        assert after_uncordon == {"status": "degraded", "cordoned": False}
        assert cordon_audited == 1
        assert uncordon_audited == 1

    asyncio.run(_run())


def test_cordon_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _NON_OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # The denied call must not have mutated the host.
        assert row == {"status": "available", "cordoned": False}

    asyncio.run(_run())


def test_set_status_denied_for_auditor_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _AUDITOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row == {"status": "available", "cordoned": False}
        assert audited == [
            ("auditor-1", "platform_auditor", "resources.set_status", f"resource:{res_id}")
        ]

    asyncio.run(_run())


def test_cordon_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.cordon_resource(pool, _NON_OPERATOR, resource_id=res_id)
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False

    asyncio.run(_run())
