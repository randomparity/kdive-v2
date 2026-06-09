"""Terminal-free coverage for the PCIe claim (ADR-0068, #162).

Occupancy is derived from non-terminal allocations' ``pcie_claim``, so a claim frees the
instant its allocation leaves the non-terminal set — there is no explicit clear. These
tests drive each real terminal path (release, lease expiry, break-glass, and the failed
state) and prove the held device becomes re-claimable. The reconciler's expiry sweep is
the orphan reaper: an orphaned non-terminal allocation past its lease is moved terminal,
freeing its claim. The audit writer is a no-op recorder (the freeing is structural, not
audit-driven).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.reconciler import loop
from kdive.security import audit
from kdive.services.allocation import pcie_claim
from kdive.services.allocation.admission import AllocationRequest, admit
from kdive.services.allocation.release import release_with_backstops

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=1, memory_gb=0, cost_class="local")
_X710 = PCIeDescriptor(
    bdf="0000:3b:00.0", vendor_id="8086", device_id="1572", class_code="020000", label="X710"
)
_SPEC = "8086:1572"


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _noop_writer(conn: psycopg.AsyncConnection, event: audit.AuditEvent) -> None:
    return None


async def _seed(conn: psycopg.AsyncConnection) -> Resource:
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
                PCIE_DEVICES_KEY: [dict(_X710)],
            },
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _admit_claim(conn: psycopg.AsyncConnection, res: Resource) -> UUID:
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=CTX, resource=res, project="proj", selector=SEL, window=1, pcie_specs=(_SPEC,)
        ),
    )
    assert outcome.granted is True and outcome.allocation is not None
    return outcome.allocation.id


async def _assert_free_and_reclaimable(conn: psycopg.AsyncConnection, res: Resource) -> None:
    assert await pcie_claim.active_claims(conn, res.id) == []
    again = await admit(
        conn,
        AllocationRequest(
            ctx=CTX, resource=res, project="proj", selector=SEL, window=1, pcie_specs=(_SPEC,)
        ),
    )
    assert again.granted is True  # the freed card is claimable by a new allocation


def test_release_frees_the_claim(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            _conn(migrated_url) as conn,
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
        ):
            res = await _seed(conn)
            alloc_id = await _admit_claim(conn, res)
            assert [c["bdf"] for c in await pcie_claim.active_claims(conn, res.id)] == [
                "0000:3b:00.0"
            ]
            outcome = await release_with_backstops(
                pool, alloc_id, project="proj", audit_writer=_noop_writer
            )
            assert outcome.released is True
            await _assert_free_and_reclaimable(conn, res)

    asyncio.run(_run())


def test_expiry_sweep_frees_the_claim(migrated_url: str) -> None:
    async def _run() -> None:
        async with (
            _conn(migrated_url) as conn,
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
        ):
            res = await _seed(conn)
            alloc_id = await _admit_claim(conn, res)
            # Push the lease into the past so the sweep reclaims it.
            await conn.execute(
                "UPDATE allocations SET lease_expiry = now() - interval '1 hour' WHERE id = %s",
                (alloc_id,),
            )
            reclaimed = await loop.reconcile_once(pool, loop.NullReaper())
            assert reclaimed.expired_allocations == 1
            stored = await ALLOCATIONS.get(conn, alloc_id)
            assert stored is not None and stored.state is AllocationState.EXPIRED
            await _assert_free_and_reclaimable(conn, res)

    asyncio.run(_run())


def test_breakglass_release_frees_the_claim(migrated_url: str) -> None:
    # Break-glass routes through the same release mechanic; the claim frees identically.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as conn,
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
        ):
            res = await _seed(conn)
            alloc_id = await _admit_claim(conn, res)
            # The break-glass writer differs only in attribution (record_system vs the
            # membership-guarded record); the release mechanic — hence the freeing — is the
            # same release_with_backstops call ops.force_release routes through.
            outcome = await release_with_backstops(
                pool, alloc_id, project="proj", audit_writer=_noop_writer
            )
            assert outcome.released is True
            await _assert_free_and_reclaimable(conn, res)

    asyncio.run(_run())


def test_failed_allocation_claim_is_not_occupied(migrated_url: str) -> None:
    # No service drives an allocation to `failed` today (it is a legal state-machine edge),
    # so this asserts the occupancy predicate excludes a `failed` allocation's claim — the
    # structural guarantee the "failure frees the claim" acceptance rests on.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed(conn)
            await ALLOCATIONS.insert(
                conn,
                Allocation(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="alice",
                    project="proj",
                    resource_id=res.id,
                    state=AllocationState.FAILED,
                    pcie_claim=[PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
                ),
            )
            await _assert_free_and_reclaimable(conn, res)

    asyncio.run(_run())


def test_orphaned_nonterminal_allocation_is_reaped_freeing_the_claim(migrated_url: str) -> None:
    # An orphaned (lease-elapsed, non-terminal) allocation holding a claim is moved
    # terminal by the reconciler expiry sweep, which frees the device.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as conn,
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
        ):
            res = await _seed(conn)
            alloc = await ALLOCATIONS.insert(
                conn,
                Allocation(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="alice",
                    project="proj",
                    resource_id=res.id,
                    state=AllocationState.GRANTED,
                    lease_expiry=datetime.now(UTC) - timedelta(hours=1),
                    requested_vcpus=1,
                    requested_memory_gb=0,
                    pcie_claim=[PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
                ),
            )
            report = await loop.reconcile_once(pool, loop.NullReaper())
            assert report.expired_allocations == 1
            stored = await ALLOCATIONS.get(conn, alloc.id)
            assert stored is not None and stored.state is AllocationState.EXPIRED
            await _assert_free_and_reclaimable(conn, res)

    asyncio.run(_run())
