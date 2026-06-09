"""resources.availability handler tests — fleet headroom / fits-now / queue depth (#163).

The handler is called directly with an injected pool (never through MCP). Hosts are
registered via the discovery fixture, then their ``capabilities`` (cap, pcie_devices) and
``cordoned``/``status`` are set with direct SQL so each case controls exactly one axis.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEM_SHAPES
from kdive.domain.models import Allocation, SystemShape
from kdive.domain.pcie import PCIeClaim
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import availability as availability_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
_DT = datetime(2026, 1, 1, tzinfo=UTC)

_X710 = {
    "bdf": "0000:3b:00.0",
    "vendor_id": "8086",
    "device_id": "1572",
    "class_code": "020000",
    "label": "Intel X710 (secret-host-label)",
}
_GPU = {
    "bdf": "0000:af:00.0",
    "vendor_id": "10de",
    "device_id": "2204",
    "class_code": "030000",
    "label": "NVIDIA",
}


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _register(pool: AsyncConnectionPool, *, host_uri: str = "qemu:///system") -> str:
    disc = LocalLibvirtDiscovery(
        host_uri=host_uri,
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


async def _set_cap(pool: AsyncConnectionPool, res_id: str, cap: object) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET capabilities = "
            "jsonb_set(capabilities, '{concurrent_allocation_cap}', %s::jsonb) WHERE id = %s",
            (json.dumps(cap), UUID(res_id)),
        )


async def _drop_cap(pool: AsyncConnectionPool, res_id: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET capabilities = capabilities - 'concurrent_allocation_cap' "
            "WHERE id = %s",
            (UUID(res_id),),
        )


async def _set_pcie(pool: AsyncConnectionPool, res_id: str, devices: list[dict]) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET capabilities = "
            "jsonb_set(capabilities, '{pcie_devices}', %s::jsonb) WHERE id = %s",
            (json.dumps(devices), UUID(res_id)),
        )


async def _set_schedulability(
    pool: AsyncConnectionPool, res_id: str, *, status: str = "available", cordoned: bool = False
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET status = %s, cordoned = %s WHERE id = %s",
            (status, cordoned, UUID(res_id)),
        )


async def _ensure_budget(pool: AsyncConnectionPool, project: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0) "
            "ON CONFLICT (project) DO NOTHING",
            (project,),
        )


async def _alloc_on(
    pool: AsyncConnectionPool,
    res_id: str | None,
    *,
    state: AllocationState,
    project: str = "tenant-x",
    pcie_claim: list[PCIeClaim] | None = None,
    requested_kind: str | None = None,
    requested_resource_id: UUID | None = None,
) -> UUID:
    await _ensure_budget(pool, project)
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="tenant-user",
                project=project,
                resource_id=UUID(res_id) if res_id is not None else None,
                state=state,
                requested_vcpus=1,
                requested_memory_gb=1,
                pcie_claim=pcie_claim or [],
                requested_kind=requested_kind,
                requested_resource_id=requested_resource_id,
            ),
        )
    return alloc.id


async def _add_shape(
    pool: AsyncConnectionPool,
    name: str,
    *,
    vcpus: int = 1,
    memory_mb: int = 1024,
    disk_gb: int = 10,
    pcie_match: str | None = None,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        await SYSTEM_SHAPES.upsert(
            conn,
            SystemShape(
                name=name,
                vcpus=vcpus,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                pcie_match=pcie_match,
                updated_at=_DT,
            ),
        )


def _host_item(resp: ToolResponse, res_id: str) -> ToolResponse:
    matches = [item for item in resp.items if item.object_id == res_id]
    assert len(matches) == 1, f"expected one item for {res_id}, got {len(matches)}"
    return matches[0]


# -- headroom -------------------------------------------------------------------------


def test_headroom_is_cap_minus_occupancy(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)  # cap=2
            await _alloc_on(pool, res_id, state=AllocationState.GRANTED)
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["cap"] == 2
        assert item.data["in_use"] == 1
        assert item.data["headroom"] == 1

    asyncio.run(_run())


def test_active_and_releasing_occupy_requested_does_not(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_cap(pool, res_id, 5)
            await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            await _alloc_on(pool, res_id, state=AllocationState.RELEASING)
            await _alloc_on(pool, res_id, state=AllocationState.GRANTED)
            # A queued requested row is excluded from occupancy (resource_id null on real
            # queued rows; here it is stamped to prove the predicate, not resource_id).
            await _alloc_on(pool, res_id, state=AllocationState.REQUESTED)
            # A terminal row never counts.
            await _alloc_on(pool, res_id, state=AllocationState.RELEASED)
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["in_use"] == 3
        assert item.data["headroom"] == 2

    asyncio.run(_run())


# -- free pcie ------------------------------------------------------------------------


def test_free_pcie_excludes_active_claims(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_pcie(pool, res_id, [_X710, _GPU])
            await _alloc_on(
                pool,
                res_id,
                state=AllocationState.ACTIVE,
                pcie_claim=[PCIeClaim(bdf=_X710["bdf"], vendor_id="8086", device_id="1572")],
            )
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        # Two descriptors, one claimed → one free.
        assert item.data["free_pcie"] == 1

    asyncio.run(_run())


def test_pcie_label_is_redacted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_pcie(pool, res_id, [_X710])
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        serialized = item.model_dump_json()
        assert "secret-host-label" not in serialized

    asyncio.run(_run())


# -- fits-now -------------------------------------------------------------------------


def test_fits_now_needs_headroom_and_free_device(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)  # cap=2
            await _set_pcie(pool, res_id, [_X710])
            await _add_shape(pool, "nic-shape", pcie_match="8086:1572")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert "nic-shape" in item.data["fits"]
        # Fleet-level fits set also carries it.
        assert "nic-shape" in resp.data["fits_now"]

    asyncio.run(_run())


def test_no_fit_when_device_absent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_pcie(pool, res_id, [_GPU])  # only a GPU, not the NIC
            await _add_shape(pool, "nic-shape", pcie_match="8086:1572")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert "nic-shape" not in item.data["fits"]

    asyncio.run(_run())


def test_no_fit_when_no_headroom(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_cap(pool, res_id, 1)
            await _alloc_on(pool, res_id, state=AllocationState.GRANTED)  # fills the host
            await _add_shape(pool, "tiny")  # no pcie requirement
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["headroom"] == 0
        assert "tiny" not in item.data["fits"]

    asyncio.run(_run())


# -- schedulability -------------------------------------------------------------------


def test_cordoned_host_is_non_schedulable_and_never_fits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_schedulability(pool, res_id, cordoned=True)
            await _add_shape(pool, "tiny")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["schedulable"] is False
        assert item.data["fits"] == []

    asyncio.run(_run())


def test_offline_host_is_non_schedulable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_schedulability(pool, res_id, status="offline")
            await _add_shape(pool, "tiny")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["schedulable"] is False
        assert item.data["fits"] == []

    asyncio.run(_run())


def test_invalid_cap_host_is_non_schedulable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _drop_cap(pool, res_id)
            await _add_shape(pool, "tiny")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        item = _host_item(resp, res_id)
        assert item.data["schedulable"] is False
        assert item.data["headroom"] == 0
        assert item.data["fits"] == []

    asyncio.run(_run())


# -- queue depth ----------------------------------------------------------------------


def test_queue_depth_counts_by_kind_and_by_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            # Two by-kind queued rows + one by-id queued row (requested_kind null).
            await _alloc_on(
                pool, None, state=AllocationState.REQUESTED, requested_kind="local-libvirt"
            )
            await _alloc_on(
                pool, None, state=AllocationState.REQUESTED, requested_kind="local-libvirt"
            )
            await _alloc_on(
                pool,
                None,
                state=AllocationState.REQUESTED,
                requested_resource_id=UUID(res_id),
            )
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape=None)
        queue = resp.data["queue_depth"]
        assert queue["total"] == 3
        assert queue["by_kind"]["local-libvirt"] == 2
        assert queue["by_id"] == 1

    asyncio.run(_run())


# -- filters --------------------------------------------------------------------------


def test_pcie_filter_narrows_to_hosts_with_free_match(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with_nic = await _register(pool, host_uri="qemu:///system")
            without_nic = await _register(pool, host_uri="qemu:///session")
            await _set_pcie(pool, with_nic, [_X710])
            await _set_pcie(pool, without_nic, [_GPU])
            resp = await availability_tools.availability_tool(
                pool, CTX, pcie="8086:1572", shape=None
            )
        ids = {item.object_id for item in resp.items}
        assert with_nic in ids
        assert without_nic not in ids

    asyncio.run(_run())


def test_pcie_filter_excludes_host_whose_match_is_claimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_pcie(pool, res_id, [_X710])
            await _alloc_on(
                pool,
                res_id,
                state=AllocationState.ACTIVE,
                pcie_claim=[PCIeClaim(bdf=_X710["bdf"], vendor_id="8086", device_id="1572")],
            )
            resp = await availability_tools.availability_tool(
                pool, CTX, pcie="8086:1572", shape=None
            )
        assert resp.items == []

    asyncio.run(_run())


def test_shape_filter_restricts_fits_to_one_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _add_shape(pool, "tiny")
            await _add_shape(pool, "also-tiny")
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape="tiny")
        item = _host_item(resp, res_id)
        assert item.data["fits"] == ["tiny"]
        assert resp.data["fits_now"] == ["tiny"]

    asyncio.run(_run())


def test_malformed_pcie_filter_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            resp = await availability_tools.availability_tool(
                pool, CTX, pcie="NOT-A-SPEC", shape=None
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_unknown_shape_filter_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            resp = await availability_tools.availability_tool(pool, CTX, pcie=None, shape="nope")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
