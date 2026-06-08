"""allocations.request PCIe selector tests — PCIe-aware selection + claim over the pool.

The MCP handler parses + grammar-validates the selector's ``pcie_devices``, does
PCIe-aware host selection (a schedulable host with a free matching device for every
spec), and threads the spec union into admission's in-lock resolve-and-claim. Config
(absent card across the fleet) and capacity (matches exist but busy) map to their typed
envelopes. Handlers called directly with an injected pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.domain.models import Budget, Quota
from kdive.domain.pcie import PCIeClaim
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle import allocations as alloc_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import Role
from kdive.services.resource_discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn, FakeNodeDevice, pci_nodedev_xml

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _x710_nodedev(function: int = 0) -> FakeNodeDevice:
    return FakeNodeDevice(
        device_name=f"pci_0000_3b_00_{function}",
        xml=pci_nodedev_xml(name=f"pci_0000_3b_00_{function}", function=function),
    )


async def _register(pool: AsyncConnectionPool, *, node_devices: list[FakeNodeDevice]) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(node_devices=node_devices),
        concurrent_allocation_cap=1000,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj", limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=1_000_000,
                max_concurrent_systems=1_000_000,
                updated_at=_DT,
            ),
        )
    return str(res.id)


async def _request(
    pool: AsyncConnectionPool, ctx: RequestContext, *, pcie_devices: list[str]
) -> ToolResponse:
    request: dict[str, Any] = {
        "vcpus": 1,
        "memory_gb": 0,
        "window": None,
        "resource": {"mode": "kind", "kind": "local-libvirt"},
        "pcie_devices": pcie_devices,
    }
    return await alloc_tools.request_allocation(pool, ctx, project="proj", request=request)


async def _claim_of(pool: AsyncConnectionPool, alloc_id: str) -> list[PCIeClaim]:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, UUID(alloc_id))
    assert alloc is not None
    return alloc.pcie_claim


def test_request_present_free_card_claims(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev()])
            resp = await _request(pool, _ctx(), pcie_devices=["8086:1572"])
            assert resp.status == "granted"
            claim = await _claim_of(pool, resp.object_id)
            assert [c["bdf"] for c in claim] == ["0000:3b:00.0"]

    asyncio.run(_run())


def test_request_absent_card_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev()])
            resp = await _request(pool, _ctx(), pcie_devices=["10de:2204"])  # GPU not present
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_request_all_busy_is_capacity_denial(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev()])
            first = await _request(pool, _ctx(), pcie_devices=["8086:1572"])
            assert first.status == "granted"
            second = await _request(pool, _ctx(), pcie_devices=["8086:1572"])
        assert second.status == "error"
        assert second.error_category == "allocation_denied"

    asyncio.run(_run())


def test_request_malformed_spec_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev()])
            resp = await _request(pool, _ctx(), pcie_devices=["NOT-A-SPEC"])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_request_multiset_claims_distinct_cards(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev(0), _x710_nodedev(1)])
            resp = await _request(pool, _ctx(), pcie_devices=["8086:1572", "8086:1572"])
            assert resp.status == "granted"
            claim = await _claim_of(pool, resp.object_id)
            assert {c["bdf"] for c in claim} == {"0000:3b:00.0", "0000:3b:00.1"}

    asyncio.run(_run())


def test_request_no_pcie_devices_is_unchanged_path(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, node_devices=[_x710_nodedev()])
            resp = await _request(pool, _ctx(), pcie_devices=[])
            assert resp.status == "granted"
            assert await _claim_of(pool, resp.object_id) == []

    asyncio.run(_run())
