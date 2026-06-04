"""The reconciler ->expired allocation sweep and idempotency-key GC (ADR-0036 §4, ADR-0040).

The sweep moves a non-terminal allocation past its lease window to ``expired``, stamps
``active_ended_at``, and writes the ``reconciled`` credit under PROJECT -> ALLOCATION (the
same lock release takes, so the two never double-reconcile). The flip orphans the
allocation's System, which the orphaned-System repair hands to the M0 teardown in the same
pass. The sweep is idempotent; a second pass is a no-op. The GC deletes idempotency_keys
rows past the retention window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, RESOURCES, SYSTEMS
from kdive.domain import accounting
from kdive.domain.models import Allocation, Budget, Resource, ResourceKind, System
from kdive.domain.state import AllocationState, ResourceStatus, SystemState
from kdive.reconciler import loop
from tests.reconciler.conftest import connect, run_repair

_DT = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_expired_alloc(
    conn: psycopg.AsyncConnection,
    *,
    state: AllocationState = AllocationState.ACTIVE,
    lease_offset: timedelta = timedelta(hours=-1),
    active_started_at: datetime | None = None,
    estimate: str = "9.0000",
    with_budget: bool = True,
) -> UUID:
    """Seed a resource + budget + allocation whose lease_expiry = now() + lease_offset."""
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
            state=state,
            requested_vcpus=2,
            requested_memory_gb=4,
            active_started_at=active_started_at,
        ),
    )
    if with_budget:
        await accounting.reserve(conn, alloc, Decimal(estimate))
    # lease_expiry is relative to the DB clock so there is no test-vs-Postgres skew.
    await conn.execute(
        "UPDATE allocations SET lease_expiry = now() + %s WHERE id = %s",
        (lease_offset, alloc.id),
    )
    return alloc.id


async def _seed_system_for(conn: psycopg.AsyncConnection, allocation_id: UUID) -> UUID:
    sys = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            allocation_id=allocation_id,
            state=SystemState.READY,
            provisioning_profile={"k": "v"},
        ),
    )
    return sys.id


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


def test_idle_expired_allocation_swept_to_expired_with_credit(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            started = datetime.now(UTC) - timedelta(hours=2)
            alloc_id = await _seed_expired_alloc(
                seed, state=AllocationState.ACTIVE, active_started_at=started
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._sweep_expired_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "expired"
            assert await _ledger_kinds(check, alloc_id) == ["reserved", "reconciled"]
            cur = await check.execute(
                "SELECT active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] is not None  # active_ended_at stamped
            budget = await check.execute("SELECT spent_kcu FROM budgets WHERE project = 'proj'")
            spent_row = await budget.fetchone()
            assert spent_row is not None
            # ~2h active at 3.0 kcu/hr -> ~6.0 net spend (9 reserved - ~3 credit).
            assert Decimal("5.9") < Decimal(spent_row[0]) < Decimal("6.1")

    asyncio.run(_run())


def test_sweep_orphans_system_for_teardown_in_one_pass(migrated_url: str) -> None:
    # After the sweep flips the allocation ->expired, the orphaned-System repair (run next
    # in reconcile_once) enqueues the M0 teardown that drains and tears the System down.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(seed, state=AllocationState.ACTIVE)
            system_id = await _seed_system_for(seed, alloc_id)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await loop.reconcile_once(pool, loop.NullReaper())
        assert report.expired_allocations == 1
        assert report.orphaned_systems == 1  # the now-expired allocation orphaned its System
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "expired"
            cur = await check.execute(
                "SELECT kind FROM jobs WHERE dedup_key = %s", (f"{system_id}:teardown",)
            )
            job = await cur.fetchone()
            assert job is not None and job[0] == "teardown"

    asyncio.run(_run())


def test_sweep_second_pass_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_expired_alloc(seed, state=AllocationState.ACTIVE)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, loop._sweep_expired_allocations)
            second = await run_repair(pool, loop._sweep_expired_allocations)
        assert first == 1
        assert second == 0  # already expired: nothing left to reclaim

    asyncio.run(_run())


def test_sweep_writes_exactly_one_reconciled_on_repeat(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(seed, state=AllocationState.GRANTED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._sweep_expired_allocations)
            await run_repair(pool, loop._sweep_expired_allocations)
        async with await connect(migrated_url) as check:
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1  # idempotent: one credit despite two passes

    asyncio.run(_run())


def test_live_lease_not_swept(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(
                seed, state=AllocationState.ACTIVE, lease_offset=timedelta(hours=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._sweep_expired_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "active"  # untouched

    asyncio.run(_run())


def test_terminal_allocation_not_swept(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(seed, state=AllocationState.RELEASED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._sweep_expired_allocations)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"

    asyncio.run(_run())


def test_unmetered_expired_allocation_swept_without_credit(migrated_url: str) -> None:
    # An allocation whose project has no budget row (never metered) still expires, but
    # reconcile is a no-op (no ledger/spent write).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(
                seed, state=AllocationState.ACTIVE, with_budget=False
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._sweep_expired_allocations)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "expired"
            assert await _ledger_kinds(check, alloc_id) == []  # no reserve, no credit

    asyncio.run(_run())


def test_idempotency_gc_deletes_old_rows_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO idempotency_keys "
                "(key, principal, project, kind, result, created_at) "
                "VALUES ('old', 'u', 'proj', 'allocations.request', '{}', "
                "now() - interval '30 days')"
            )
            await seed.execute(
                "INSERT INTO idempotency_keys "
                "(key, principal, project, kind, result, created_at) "
                "VALUES ('fresh', 'u', 'proj', 'allocations.renew', '{}', now())"
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            deleted = await run_repair(
                pool, lambda conn: loop._gc_idempotency_keys(conn, timedelta(days=7))
            )
        assert deleted == 1  # only the 30-day-old row
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT key FROM idempotency_keys ORDER BY key")
            rows = [r[0] for r in await cur.fetchall()]
        assert rows == ["fresh"]

    asyncio.run(_run())


def test_reconcile_once_reports_gc_count(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO idempotency_keys "
                "(key, principal, project, kind, result, created_at) "
                "VALUES ('stale', 'u', 'proj', 'allocations.request', '{}', "
                "now() - interval '99 days')"
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await loop.reconcile_once(pool, loop.NullReaper())
        assert report.idempotency_keys_gcd == 1

    asyncio.run(_run())


def test_concurrent_release_vs_sweep_reconciles_once(migrated_url: str) -> None:
    # The sweep and release both take PROJECT -> ALLOCATION; whichever reaches the
    # allocation lock first wins. Here a release pre-holds the locks and reconciles +
    # releases the allocation while the sweep blocks; the sweep then sees a terminal
    # state and skips, so exactly one reconciled row exists.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            alloc_id = await _seed_expired_alloc(seed, state=AllocationState.GRANTED)
        holder = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        try:
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                async with (
                    holder.transaction(),
                    advisory_xact_lock(holder, LockScope.PROJECT, "proj"),
                    advisory_xact_lock(holder, LockScope.ALLOCATION, alloc_id),
                ):
                    task = asyncio.ensure_future(run_repair(pool, loop._sweep_expired_allocations))
                    await asyncio.sleep(0.3)
                    assert not task.done()  # blocked behind the held locks
                    await ALLOCATIONS.update_state(holder, alloc_id, AllocationState.RELEASING)
                    alloc = await ALLOCATIONS.update_state(
                        holder, alloc_id, AllocationState.RELEASED
                    )
                    await accounting.reconcile(holder, alloc)
                count = await task
        finally:
            await holder.close()
        assert count == 0  # the sweep lost the race and skipped the now-terminal allocation
        async with await connect(migrated_url) as check:
            assert await _alloc_state(check, alloc_id) == "released"
            reconciled = [k for k in await _ledger_kinds(check, alloc_id) if k == "reconciled"]
            assert len(reconciled) == 1

    asyncio.run(_run())
