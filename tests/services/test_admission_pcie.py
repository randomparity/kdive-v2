"""Tests for the in-lock PCIe resolve-and-claim in M1 admission (ADR-0068, #162).

The claim is a locked read-modify-write under the per-Resource lock: admission resolves
the requested spec union to distinct free devices and persists ``allocations.pcie_claim``
in the same transaction it grants, or denies (config vs. capacity) with no durable write.
Budget/quota are seeded generous so PCIe is the binding constraint. Real Postgres.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.services.allocation_admission import AllocationRequest, admit
from tests.db_waits import wait_until_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=1, memory_gb=0, cost_class="local")

_X710 = PCIeDescriptor(
    bdf="0000:3b:00.0", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)
_X710_B = PCIeDescriptor(
    bdf="0000:3b:00.1", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_budget_quota(conn: psycopg.AsyncConnection) -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal("1000000"), spent_kcu=Decimal(0), updated_at=_DT),
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


async def _seed_resource(conn: psycopg.AsyncConnection, devices: list[PCIeDescriptor]) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: 1000,
                "vcpus": 64,
                "memory_mb": 65536,
                PCIE_DEVICES_KEY: [dict(d) for d in devices],
            },
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


def _admit(conn: psycopg.AsyncConnection, resource: Resource, specs: tuple[str, ...]):  # type: ignore[no-untyped-def]
    return admit(
        conn,
        AllocationRequest(
            ctx=CTX,
            resource=resource,
            project="proj",
            selector=SEL,
            window=1,
            pcie_specs=specs,
        ),
    )


async def _count_allocs(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM allocations")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_admit_claims_single_device(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res, ("8086:1572",))
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.pcie_claim == [
                {"bdf": "0000:3b:00.0", "vendor_id": "8086", "device_id": "1572"}
            ]
            stored = await ALLOCATIONS.get(conn, outcome.allocation.id)
            assert stored is not None and stored.pcie_claim[0]["bdf"] == "0000:3b:00.0"

    asyncio.run(_run())


def test_admit_claims_multiset_distinct_devices(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710, _X710_B])
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res, ("8086:1572", "8086:1572"))
            assert outcome.granted is True
            assert outcome.allocation is not None
            bdfs = {d["bdf"] for d in outcome.allocation.pcie_claim}
            assert bdfs == {"0000:3b:00.0", "0000:3b:00.1"}  # distinct cards

    asyncio.run(_run())


def test_admit_no_pcie_specs_leaves_empty_claim(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res, ())
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.pcie_claim == []

    asyncio.run(_run())


def test_admit_absent_card_is_config_error_no_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res, ("10de:2204",))  # GPU not present
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count_allocs(conn) == 0  # fail-closed: no durable write

    asyncio.run(_run())


def test_admit_all_busy_is_capacity_denial(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            first = await _admit(conn, res, ("8086:1572",))
            assert first.granted is True
            second = await _admit(conn, res, ("8086:1572",))  # only card now claimed
            assert second.granted is False
            assert second.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count_allocs(conn) == 1  # the busy retry wrote nothing

    asyncio.run(_run())


def test_admit_malformed_spec_is_config_error_no_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res, ("not-a-spec",))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count_allocs(conn) == 0

    asyncio.run(_run())


async def _spent_kcu(conn: psycopg.AsyncConnection) -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute("SELECT spent_kcu FROM budgets WHERE project = 'proj'")
        row = await cur.fetchone()
    assert row is not None
    return Decimal(row[0])


def test_claim_does_not_change_kcu_estimate(migrated_url: str) -> None:
    # PCIe is not a cost input (ADR-0068): the reserved spend for a sized request is
    # identical whether or not it claims a device. Two hosts so neither claim collides.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res_plain = await _seed_resource(conn, [])
            res_pcie = await _seed_resource(conn, [_X710])
            await _seed_budget_quota(conn)
            before = await _spent_kcu(conn)
            plain = await _admit(conn, res_plain, ())
            after_plain = await _spent_kcu(conn)
            pcie = await _admit(conn, res_pcie, ("8086:1572",))
            after_pcie = await _spent_kcu(conn)
            assert plain.granted is True and pcie.granted is True
            # Each grant reserved the same delta — the claim added nothing to the cost.
            assert after_plain - before == after_pcie - after_plain

    asyncio.run(_run())


def test_admit_does_not_double_book_last_device_under_concurrency(migrated_url: str) -> None:
    # Two admits race the single free card through the RESOURCE lock: the holder pre-locks
    # the resource so admit on conn B blocks; once an existing claim is committed the
    # in-lock re-resolve sees the card busy and denies CAPACITY — never a double-book.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as seed,
            _conn(migrated_url) as a,
            _conn(migrated_url) as b,
        ):
            res = await _seed_resource(seed, [_X710])
            await _seed_budget_quota(seed)
            # Pre-existing committed claim on the only card (simulates the race winner).
            await ALLOCATIONS.insert(
                seed,
                Allocation(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="alice",
                    project="proj",
                    resource_id=res.id,
                    state=AllocationState.GRANTED,
                    pcie_claim=[PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
                ),
            )
            async with a.transaction(), advisory_xact_lock(a, LockScope.RESOURCE, res.id):
                task = asyncio.ensure_future(_admit(b, res, ("8086:1572",)))
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()  # blocked on the resource lock
            outcome = await task
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED

    asyncio.run(_run())
