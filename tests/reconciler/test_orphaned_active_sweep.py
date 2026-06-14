"""The reconciler orphaned-`active` allocation reaper (ADR-0108, #371).

A failed/interrupted lifecycle run leaves an allocation `active` while its single System has
reached a terminal state (`torn_down`/`failed`) — the teardown job never releases the
allocation — so it permanently holds its host-cap slot and wedges a `cap=1` host. The reaper
releases such an allocation (`active -> releasing -> released`, with the `active_ended_at`
stamp and the single `reconciled` credit), but preserves an allocation whose System is still
live (including `crashed`, an in-progress crash investigation), and waits out a grace window
to avoid a mid-provision race. It is idempotent and per-candidate isolated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES, SYSTEMS
from kdive.domain.cost import Selector
from kdive.domain.models import (
    Allocation,
    Budget,
    Quota,
    Resource,
    ResourceKind,
    System,
)
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus, SystemState
from kdive.mcp.auth import RequestContext
from kdive.providers.reaping import NullReaper
from kdive.reconciler import allocations as allocation_repairs
from kdive.reconciler import loop
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation.admission import AllocationRequest, admit
from tests.db_waits import wait_until_any_backend_waiting
from tests.reconciler.conftest import connect, run_repair

_DT = datetime(2026, 1, 1, tzinfo=UTC)


async def _age_updated_at(conn: psycopg.AsyncConnection, alloc_id: UUID, age: timedelta) -> None:
    """Set ``allocations.updated_at = now() - age``, bypassing the set_updated_at trigger.

    The ``allocations_set_updated_at`` trigger rewrites ``updated_at := now()`` on every
    row-changing UPDATE, so a plain UPDATE cannot age it. The test disables that trigger for
    the single aging statement to simulate an allocation whose last write was ``age`` ago.
    """
    await conn.execute("ALTER TABLE allocations DISABLE TRIGGER allocations_set_updated_at")
    try:
        await conn.execute(
            "UPDATE allocations SET updated_at = now() - %s WHERE id = %s", (age, alloc_id)
        )
    finally:
        await conn.execute("ALTER TABLE allocations ENABLE TRIGGER allocations_set_updated_at")


async def _seed_active_alloc(
    conn: psycopg.AsyncConnection,
    *,
    system_state: SystemState | None = SystemState.TORN_DOWN,
    updated_age: timedelta = timedelta(minutes=10),
    with_budget: bool = True,
    sized: bool = True,
) -> UUID:
    """Seed resource (+budget) -> active allocation (-> System in ``system_state``).

    ``system_state=None`` seeds no System row. ``updated_age`` ages the allocation's
    ``updated_at`` to ``now() - updated_age`` (set in SQL, DB clock). The allocation is sized
    and reserved so a release writes a real ``reconciled`` credit.
    """
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="p",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    if with_budget:
        await BUDGETS.upsert(
            conn,
            Budget(project="proj", limit_kcu=Decimal("1000"), spent_kcu=Decimal(0), updated_at=_DT),
        )
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=AllocationState.ACTIVE,
            requested_vcpus=2 if sized else None,
            requested_memory_gb=4 if sized else None,
            active_started_at=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    if with_budget:
        await accounting.reserve(conn, alloc, Decimal("9.0000"))
    if system_state is not None:
        await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                allocation_id=alloc.id,
                state=system_state,
                provisioning_profile={"k": "v"},
            ),
        )
    # Age updated_at in SQL so there is no test-vs-Postgres clock skew. Done last because each
    # insert/update bumps updated_at (via the trigger this helper bypasses).
    await _age_updated_at(conn, alloc.id, updated_age)
    return alloc.id


async def _alloc_state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _ledger_kinds(conn: psycopg.AsyncConnection, alloc_id: UUID) -> list[str]:
    cur = await conn.execute(
        "SELECT event_type FROM ledger WHERE allocation_id = %s ORDER BY ts, id", (alloc_id,)
    )
    return [r[0] for r in await cur.fetchall()]


def test_leaked_active_with_torn_down_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            assert await _ledger_kinds(check, alloc_id) == ["reserved", "reconciled"]
            cur = await check.execute(
                "SELECT active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] is not None  # active_ended_at stamped

    asyncio.run(_run())


def test_leaked_active_with_failed_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.FAILED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_leaked_active_with_no_system_reclaimed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=None)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_active_with_ready_system_preserved(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.READY)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"  # untouched

    asyncio.run(_run())


def test_active_with_provisioning_system_preserved(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.PROVISIONING)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_active_with_crashed_system_preserved(migrated_url: str) -> None:
    # A `crashed` System whose allocation backs an in-progress crash investigation is LIVE,
    # not orphaned — the central kdive workflow. Reaping its slot would be the worst false
    # positive, so it must be preserved.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.CRASHED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"

    asyncio.run(_run())


def test_within_grace_preserved_then_reclaimed_after_aging(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            # Freshly settled (updated_at ~ now): inside the 2-min grace window.
            alloc_id = await _seed_active_alloc(
                seed, system_state=SystemState.TORN_DOWN, updated_age=timedelta(seconds=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
            assert first == 0  # within grace
            async with await connect(migrated_url) as age:
                await _age_updated_at(age, alloc_id, timedelta(minutes=10))
            second = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert second == 1  # past grace now
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_reap_second_pass_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
            second = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert first == 1
        assert second == 0  # already released
        async with await connect(migrated_url) as check:
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1  # idempotent: one credit despite two passes

    asyncio.run(_run())


def test_unpriceable_leaked_active_does_not_starve_siblings(migrated_url: str) -> None:
    # An unsized active allocation cannot be reconciled; its per-candidate transaction rolls
    # back and it stays `active` for retry, while a valid sibling is still reclaimed.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            bad = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN, sized=False)
            good = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
        assert count == 1  # only the good one
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, good) == "released"
            assert await _alloc_state(check, bad) == "active"  # rolled back, retried next pass

    asyncio.run(_run())


def test_concurrent_release_vs_reap_reconciles_once(migrated_url: str) -> None:
    # The reaper and a release both take PROJECT -> ALLOCATION. A holder pre-takes the locks
    # and releases the allocation while the reaper blocks; the reaper then sees a terminal
    # state under the lock and skips, so exactly one reconciled row exists.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_active_alloc(seed, system_state=SystemState.TORN_DOWN)
        holder = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        try:
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.PROJECT, "proj"),
                    advisory_xact_lock(holder, LockScope.ALLOCATION, alloc_id),
                ):
                    task = asyncio.ensure_future(
                        run_repair(pool, allocation_repairs.reap_orphaned_active_allocations)
                    )
                    await wait_until_any_backend_waiting(holder, locktype="advisory")
                    assert not task.done()  # blocked behind the held locks
                    await ALLOCATIONS.update_state(holder, alloc_id, AllocationState.RELEASING)
                    alloc = await ALLOCATIONS.update_state(
                        holder, alloc_id, AllocationState.RELEASED
                    )
                    await accounting.reconcile(holder, alloc)
                count = await task
        finally:
            await holder.close()
        assert count == 0  # the reaper lost the race and skipped the now-terminal allocation
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1

    asyncio.run(_run())


async def _seed_capped_resource(conn: psycopg.AsyncConnection) -> Resource:
    """A cap=1 resource with the capabilities + project quota the promotion sweep needs."""
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: 1, "vcpus": 64, "memory_mb": 65536},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
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
            max_pending_allocations=100,
            updated_at=_DT,
        ),
    )
    return resource


async def _seed_leaked_active_on(conn: psycopg.AsyncConnection, resource: Resource) -> UUID:
    """An active allocation on ``resource`` whose System is torn_down, aged past grace."""
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=AllocationState.ACTIVE,
            requested_vcpus=1,
            requested_memory_gb=0,
            active_started_at=datetime.now(UTC) - timedelta(hours=1),
        ),
    )
    await accounting.reserve(conn, alloc, Decimal("9.0000"))
    await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            allocation_id=alloc.id,
            state=SystemState.TORN_DOWN,
            provisioning_profile={"k": "v"},
        ),
    )
    await _age_updated_at(conn, alloc.id, timedelta(minutes=10))
    return alloc.id


async def _enqueue_request(conn: psycopg.AsyncConnection, resource: Resource) -> UUID:
    """Queue a `requested` row on the at-capacity ``resource`` via admit(on_capacity=queue)."""
    ctx = RequestContext(principal="bob", agent_session="sess", projects=("proj",))
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=ctx,
            resource=resource,
            project="proj",
            selector=Selector(vcpus=1, memory_gb=0, cost_class="local"),
            window=1,
            on_capacity="queue",
            disk_gb=10,
            requested_kind=ResourceKind.LOCAL_LIBVIRT,
        ),
    )
    assert outcome.granted and outcome.allocation is not None
    assert outcome.allocation.state is AllocationState.REQUESTED
    return outcome.allocation.id


def test_reconcile_once_reports_counter_and_frees_slot_same_pass(migrated_url: str) -> None:
    # One reconcile_once pass reaps the leaked active allocation and the promotion sweep
    # (run right after) fills the freed cap=1 slot with a queued request on the same resource.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            resource = await _seed_capped_resource(seed)
            leaked = await _seed_leaked_active_on(seed, resource)
            queued = await _enqueue_request(seed, resource)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await loop.reconcile_once(pool, NullReaper())
        assert report.reaped_active_allocations == 1
        assert report.promoted_allocations == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, leaked) == "released"
            assert await _alloc_state(check, queued) == "granted"  # filled the freed slot

    asyncio.run(_run())
