"""Tests for the admission-side PCIe resolve/claim helpers (ADR-0068, #162).

These exercise the pure-logic seams the in-lock claim composes: reading + validating a
host's static descriptors from ``capabilities``, deriving the occupancy set from the
host's non-terminal allocations' claims, and resolving a requested spec union to distinct
free devices with the config-vs-capacity split. Real Postgres for ``active_claims`` (it
queries allocations); the rest is in-memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, MatchOutcome, PCIeClaim, PCIeDescriptor
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.services import pcie_claim

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_X710 = PCIeDescriptor(
    bdf="0000:3b:00.0", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)
_X710_B = PCIeDescriptor(
    bdf="0000:3b:00.1", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)
_GPU = PCIeDescriptor(
    bdf="0000:65:00.0", vendor_id="10de", device_id="2204", class_code="030000", label="GPU"
)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def _resource(devices: object) -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={PCIE_DEVICES_KEY: devices},
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


# --- descriptors_for -------------------------------------------------------------------


def test_descriptors_for_reads_well_formed_list() -> None:
    out = pcie_claim.descriptors_for(_resource([dict(_X710), dict(_GPU)]))
    assert [d["bdf"] for d in out] == ["0000:3b:00.0", "0000:65:00.0"]


def test_descriptors_for_absent_key_is_empty() -> None:
    res = _resource([])
    res.capabilities.pop(PCIE_DEVICES_KEY)
    assert pcie_claim.descriptors_for(res) == []


def test_descriptors_for_skips_malformed_entries() -> None:
    # A non-dict, a dict missing fields, and a dict with non-string fields are dropped;
    # the host-derived list is untrusted, so one bad entry never blanks the inventory.
    out = pcie_claim.descriptors_for(
        _resource(
            [
                "not-a-dict",
                {"bdf": "x", "vendor_id": "8086"},  # missing fields
                {**dict(_X710), "vendor_id": 8086},  # non-string
                dict(_X710),
            ]
        )
    )
    assert [d["bdf"] for d in out] == ["0000:3b:00.0"]


def test_descriptors_for_non_list_is_empty() -> None:
    assert pcie_claim.descriptors_for(_resource({"not": "a list"})) == []


# --- resolve_union ---------------------------------------------------------------------


def test_resolve_union_single_match() -> None:
    out = pcie_claim.resolve_union(["8086:1572"], [_X710], claims=[])
    assert out.outcome is MatchOutcome.MATCHED
    assert [d["bdf"] for d in out.devices] == ["0000:3b:00.0"]


def test_resolve_union_multiset_distinct() -> None:
    out = pcie_claim.resolve_union(["8086:1572", "8086:1572"], [_X710, _X710_B], claims=[])
    assert out.outcome is MatchOutcome.MATCHED
    assert {d["bdf"] for d in out.devices} == {"0000:3b:00.0", "0000:3b:00.1"}


def test_resolve_union_absent_card_is_config() -> None:
    out = pcie_claim.resolve_union(["8086:1572"], [_GPU], claims=[])
    assert out.outcome is MatchOutcome.CONFIG


def test_resolve_union_all_busy_is_capacity() -> None:
    claim = PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")
    out = pcie_claim.resolve_union(["8086:1572"], [_X710], claims=[claim])
    assert out.outcome is MatchOutcome.CAPACITY


def test_resolve_union_empty_specs_matches_no_devices() -> None:
    out = pcie_claim.resolve_union([], [_X710], claims=[])
    assert out.outcome is MatchOutcome.MATCHED
    assert out.devices == []


# --- active_claims ---------------------------------------------------------------------


async def _seed_alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    state: AllocationState,
    claim: list[PCIeClaim],
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource_id,
            state=state,
            pcie_claim=claim,
        ),
    )


async def _seed_host(conn: psycopg.AsyncConnection) -> Resource:
    return await RESOURCES.insert(conn, _resource([dict(_X710), dict(_X710_B)]))


def test_active_claims_unions_non_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            await _seed_alloc(
                conn,
                host.id,
                AllocationState.GRANTED,
                [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
            )
            claims = await pcie_claim.active_claims(conn, host.id)
            assert [c["bdf"] for c in claims] == ["0000:3b:00.0"]

    asyncio.run(_run())


def test_active_claims_excludes_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            for state in (
                AllocationState.RELEASED,
                AllocationState.EXPIRED,
                AllocationState.FAILED,
            ):
                await _seed_alloc(
                    conn,
                    host.id,
                    state,
                    [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
                )
            assert await pcie_claim.active_claims(conn, host.id) == []

    asyncio.run(_run())


def test_active_claims_counts_releasing(migrated_url: str) -> None:
    # RELEASING is non-terminal: its claim still occupies the device (challenge finding 2).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            await _seed_alloc(
                conn,
                host.id,
                AllocationState.RELEASING,
                [PCIeClaim(bdf="0000:3b:00.1", vendor_id="8086", device_id="1572")],
            )
            claims = await pcie_claim.active_claims(conn, host.id)
            assert [c["bdf"] for c in claims] == ["0000:3b:00.1"]

    asyncio.run(_run())


def test_active_claims_is_host_scoped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            host = await _seed_host(conn)
            other = await _seed_host(conn)
            await _seed_alloc(
                conn,
                other.id,
                AllocationState.GRANTED,
                [PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
            )
            assert await pcie_claim.active_claims(conn, host.id) == []

    asyncio.run(_run())
