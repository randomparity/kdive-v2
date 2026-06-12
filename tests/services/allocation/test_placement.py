"""Behavior tests for allocation placement candidate resolution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.services.allocation.placement import PlacementRequest, resolve_placement_candidates

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_NIC = PCIeDescriptor(
    bdf="0000:01:00.0",
    vendor_id="8086",
    device_id="1572",
    class_code="020000",
    label="x710",
)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _resource(
    conn: psycopg.AsyncConnection,
    *,
    created_offset: timedelta = timedelta(0),
    status: ResourceStatus = ResourceStatus.AVAILABLE,
    cordoned: bool = False,
    pcie: bool = False,
) -> Resource:
    capabilities: dict[str, object] = {
        CONCURRENT_ALLOCATION_CAP_KEY: 10,
        "vcpus": 64,
        "memory_mb": 65536,
    }
    if pcie:
        capabilities[PCIE_DEVICES_KEY] = [_NIC]
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT + created_offset,
            updated_at=_DT + created_offset,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities=capabilities,
            pool="local-libvirt",
            cost_class="local",
            status=status,
            host_uri="qemu:///system",
            cordoned=cordoned,
        ),
    )
    created_at = _DT + created_offset
    await conn.execute(
        "UPDATE resources SET created_at = %s, updated_at = %s WHERE id = %s",
        (created_at, created_at, resource.id),
    )
    refreshed = await RESOURCES.get(conn, resource.id)
    assert refreshed is not None
    return refreshed


async def _claim(conn: psycopg.AsyncConnection, resource_id: UUID) -> None:
    await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=AllocationState.GRANTED,
            pcie_claim=[
                PCIeClaim(bdf=_NIC["bdf"], vendor_id=_NIC["vendor_id"], device_id=_NIC["device_id"])
            ],
        ),
    )


def test_explicit_resource_selection_rejects_unschedulable_hosts(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            available = await _resource(conn)
            cordoned = await _resource(conn, cordoned=True)
            unavailable = await _resource(conn, status=ResourceStatus.OFFLINE)

            selected = await resolve_placement_candidates(
                conn, PlacementRequest(resource_id=available.id)
            )
            cordoned_result = await resolve_placement_candidates(
                conn, PlacementRequest(resource_id=cordoned.id)
            )
            unavailable_result = await resolve_placement_candidates(
                conn, PlacementRequest(resource_id=unavailable.id)
            )

        assert [resource.id for resource in selected.resources] == [available.id]
        assert cordoned_result.resources == []
        assert unavailable_result.resources == []

    asyncio.run(_run())


def test_kind_candidates_are_schedulable_and_created_ordered(migrated_url: str) -> None:
    async def _run() -> tuple[list[UUID], UUID, UUID]:
        async with _conn(migrated_url) as conn:
            newer = await _resource(conn, created_offset=timedelta(minutes=2))
            await _resource(conn, created_offset=timedelta(minutes=1), cordoned=True)
            await _resource(
                conn,
                created_offset=timedelta(minutes=3),
                status=ResourceStatus.OFFLINE,
            )
            older = await _resource(conn)
            candidates = await resolve_placement_candidates(
                conn, PlacementRequest(resource_id=None, kind=ResourceKind.LOCAL_LIBVIRT)
            )
        return [resource.id for resource in candidates.resources], older.id, newer.id

    ids, older_id, newer_id = asyncio.run(_run())
    assert ids == [older_id, newer_id]


def test_pcie_resolution_reports_busy_capacity_candidate(migrated_url: str) -> None:
    async def _run() -> tuple[list[UUID], UUID | None, UUID, UUID]:
        async with _conn(migrated_url) as conn:
            busy = await _resource(conn, pcie=True)
            free = await _resource(conn, created_offset=timedelta(minutes=1), pcie=True)
            await _claim(conn, busy.id)
            candidates = await resolve_placement_candidates(
                conn,
                PlacementRequest(
                    resource_id=None,
                    kind=ResourceKind.LOCAL_LIBVIRT,
                    pcie_specs=("8086:1572",),
                ),
            )
        ids = [resource.id for resource in candidates.resources]
        capacity_id = candidates.capacity_candidate.id if candidates.capacity_candidate else None
        return ids, capacity_id, free.id, busy.id

    matched_ids, capacity_id, free_id, busy_id = asyncio.run(_run())
    assert matched_ids == [free_id]
    assert capacity_id == busy_id
