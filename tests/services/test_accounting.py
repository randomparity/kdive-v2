"""Metering-ledger tests: reserve/reconcile net to actual, spent_kcu == ledger Σ.

Real Postgres (the `migrated_url` fixture); `domain.accounting` functions called
directly on injected connections. The invariants (ADR-0007 §3): a `reserve` then
`reconcile` for a known active duration net to `rate × active_hours`; `spent_kcu`
stays equal to the ledger Σ (the O(1) running total never drifts from the audit trail);
release from `granted` (never active) → a full credit; `usage`/`usage_for_investigation`
roll up without double-counting a shared allocation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import (
    ALLOCATIONS,
    BUDGETS,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    Allocation,
    Budget,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.services import accounting
from tests.db_waits import wait_until_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(conn: psycopg.AsyncConnection) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_budget(
    conn: psycopg.AsyncConnection, project: str = "proj", *, limit: str = "1000"
) -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project=project, limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
    )


async def _seed_alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    *,
    state: AllocationState = AllocationState.ACTIVE,
    project: str = "proj",
    vcpus: int | None = 2,
    memory_gb: int | None = 4,
    active_started_at: datetime | None = None,
    active_ended_at: datetime | None = None,
) -> Allocation:
    return await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=project,
            resource_id=resource_id,
            state=state,
            requested_vcpus=vcpus,
            requested_memory_gb=memory_gb,
            active_started_at=active_started_at,
            active_ended_at=active_ended_at,
        ),
    )


async def _spent(conn: psycopg.AsyncConnection, project: str = "proj") -> Decimal:
    budget = await BUDGETS.get(conn, project)
    assert budget is not None
    return budget.spent_kcu


async def _ledger_sum(conn: psycopg.AsyncConnection, project: str = "proj") -> Decimal:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(kcu_delta), 0) FROM ledger WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    assert row is not None
    return Decimal(row[0])


async def _seed_investigation(
    conn: psycopg.AsyncConnection, project: str = "proj"
) -> Investigation:
    return await INVESTIGATIONS.insert(
        conn,
        Investigation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=project,
            title="inv",
            state=InvestigationState.OPEN,
        ),
    )


async def _seed_run_chain(
    conn: psycopg.AsyncConnection, allocation: Allocation, investigation_id: UUID
) -> None:
    """Wire an Allocation to an Investigation through a System and a Run."""
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=allocation.project,
            allocation_id=allocation.id,
            state=SystemState.READY,
            provisioning_profile={},
        ),
    )
    await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=allocation.project,
            investigation_id=investigation_id,
            system_id=system.id,
            state=RunState.CREATED,
            build_profile={},
        ),
    )


# --- reserve ---------------------------------------------------------------


def test_reserve_writes_row_and_increments_spent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(conn, res.id)
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            assert await _spent(conn) == Decimal("9.0000")
            assert await _ledger_sum(conn) == Decimal("9.0000")
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT event_type, kcu_delta, resource_id FROM ledger "
                    "WHERE allocation_id = %s",
                    (alloc.id,),
                )
                rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "reserved"
            assert Decimal(rows[0][1]) == Decimal("9.0000")
            assert rows[0][2] == res.id

    asyncio.run(_run())


# --- reconcile -------------------------------------------------------------


def test_reserve_then_reconcile_net_to_rate_times_active_hours(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            # rate = 1.0*(1*2 + 0.25*4) = 3.0 kcu/hr. Reserve a 3h window = 9.0; run 2h.
            alloc = await _seed_alloc(
                conn,
                res.id,
                active_started_at=_DT,
                active_ended_at=_DT + timedelta(hours=2),
            )
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            delta = await accounting.reconcile(conn, alloc)
            # actual = 3.0 * 2 = 6.0; delta = 6.0 - 9.0 = -3.0; net spent = 6.0.
            assert delta == Decimal("-3.0000")
            assert await _spent(conn) == Decimal("6.0000")
            assert await _ledger_sum(conn) == Decimal("6.0000")  # spent == ledger Σ

    asyncio.run(_run())


def test_reconcile_sums_all_reserved_rows(migrated_url: str) -> None:
    # A renewal writes an additional reserved row; reconcile nets against their Σ, not
    # the initial estimate, so a renewed allocation is not permanently over-debited.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(
                conn, res.id, active_started_at=_DT, active_ended_at=_DT + timedelta(hours=4)
            )
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            await accounting.reserve(conn, alloc, Decimal("9.0000"))  # renewal
            delta = await accounting.reconcile(conn, alloc)
            # actual = 3.0 * 4 = 12.0; Σ reserved = 18.0; delta = -6.0; net = 12.0.
            assert delta == Decimal("-6.0000")
            assert await _spent(conn) == Decimal("12.0000")
            assert await _ledger_sum(conn) == Decimal("12.0000")

    asyncio.run(_run())


def test_reconcile_from_granted_never_active_is_full_credit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            # Never went active: active_started_at is null → active_hours = 0.
            alloc = await _seed_alloc(conn, res.id, state=AllocationState.GRANTED)
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            delta = await accounting.reconcile(conn, alloc)
            assert delta == Decimal("-9.0000")  # full credit
            assert await _spent(conn) == Decimal("0.0000")
            assert await _ledger_sum(conn) == Decimal("0.0000")

    asyncio.run(_run())


def test_reconcile_credit_has_nullable_resource_id_for_unprovisioned(migrated_url: str) -> None:
    # An allocation released from granted never had a System; the reconciled credit's
    # resource_id is still the allocation's resource (it was chosen at request). The
    # nullable column matters for a credit that has no resource at all (defensive path).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(conn, res.id, state=AllocationState.GRANTED)
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            await accounting.reconcile(conn, alloc)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT event_type FROM ledger WHERE allocation_id = %s ORDER BY ts",
                    (alloc.id,),
                )
                rows = await cur.fetchall()
            assert [r[0] for r in rows] == ["reserved", "reconciled"]

    asyncio.run(_run())


def test_reconcile_zero_ledger_no_reservation_is_zero_delta(migrated_url: str) -> None:
    # Defensive: reconcile an allocation that never had a reserved row (Σ = 0). actual is
    # also 0 (granted, never active) → delta 0, one reconciled row written, spent unchanged.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(conn, res.id, state=AllocationState.GRANTED)
            delta = await accounting.reconcile(conn, alloc)
            assert delta == Decimal("0.0000")
            assert await _spent(conn) == Decimal("0.0000")
            assert await _ledger_sum(conn) == Decimal("0.0000")

    asyncio.run(_run())


def test_reconcile_missing_size_fails_closed(migrated_url: str) -> None:
    # An active allocation with null requested size cannot price actual → configuration_error.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(
                conn,
                res.id,
                vcpus=None,
                memory_gb=None,
                active_started_at=_DT,
                active_ended_at=_DT + timedelta(hours=1),
            )
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            try:
                await accounting.reconcile(conn, alloc)
                raise AssertionError("expected CategorizedError")
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


# --- stamp_active_ended ----------------------------------------------------


async def _db_active_interval(
    conn: psycopg.AsyncConnection, alloc_id: UUID
) -> tuple[datetime | None, datetime | None]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT active_started_at, active_ended_at FROM allocations WHERE id = %s",
            (alloc_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_stamp_active_ended_rereads_committed_started_at(migrated_url: str) -> None:
    # The release/expire path reads an allocation snapshot, then provision-ready commits
    # active_started_at before the stamp decision (#84). stamp_active_ended must decide
    # from committed DB state, not the stale snapshot, so it stamps active_ended_at and
    # returns the committed started_at — otherwise the interval stays open (active_hours=0).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            # Snapshot read while still granted-but-not-yet-active: active_started_at NULL.
            stale = await _seed_alloc(conn, res.id, active_started_at=None)
            # provision-ready commits the start stamp out of band (the racing writer).
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE allocations SET active_started_at = %s WHERE id = %s",
                    (_DT, stale.id),
                )
            ended = _DT + timedelta(hours=2)
            result = await accounting.stamp_active_ended(conn, stale, ended)
            db_started, db_ended = await _db_active_interval(conn, stale.id)
            assert db_ended == ended  # interval closed in the DB
            assert result.active_started_at == _DT  # committed start carried into the model
            assert result.active_ended_at == ended
            assert db_started == _DT

    asyncio.run(_run())


def test_stamp_active_ended_then_reconcile_bills_nonzero(migrated_url: str) -> None:
    # End to end of #84: with the start committed out of band, stamp + reconcile must
    # price active_hours > 0 (a partial credit), not the full-credit under-bill.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            stale = await _seed_alloc(conn, res.id, active_started_at=None)
            await accounting.reserve(conn, stale, Decimal("9.0000"))
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE allocations SET active_started_at = %s WHERE id = %s",
                    (_DT, stale.id),
                )
            result = await accounting.stamp_active_ended(conn, stale, _DT + timedelta(hours=2))
            delta = await accounting.reconcile(conn, result)
            # rate = 3.0/hr, 2h → actual 6.0; delta = 6.0 - 9.0 = -3.0 (NOT a -9.0 full credit).
            assert delta == Decimal("-3.0000")
            assert await _spent(conn) == Decimal("6.0000")

    asyncio.run(_run())


def test_stamp_active_ended_blocks_on_inflight_start(migrated_url: str) -> None:
    # The ->expired sweep stamps before it takes the allocation row lock, so it can run
    # while the provision-ready writer's active_started_at stamp is in flight (row-locked,
    # uncommitted) in another transaction. stamp_active_ended must block on that row lock
    # and, once the writer commits, observe the committed start and close the interval —
    # not skip it (#84). A snapshot-predicated UPDATE would not block and would under-bill.
    async def _run() -> None:
        async with _conn(migrated_url) as setup:
            res = await _seed_resource(setup)
            alloc = await _seed_alloc(setup, res.id, active_started_at=None)
        ended = _DT + timedelta(hours=2)
        writer = await psycopg.AsyncConnection.connect(migrated_url)
        stamper = await psycopg.AsyncConnection.connect(migrated_url)
        try:

            async def _do_stamp() -> Allocation:
                async with stamper.transaction():
                    return await accounting.stamp_active_ended(stamper, alloc, ended)

            async with writer.transaction():
                # provision-ready holds the row lock with an uncommitted start stamp
                await writer.execute(
                    "UPDATE allocations SET active_started_at = %s "
                    "WHERE id = %s AND active_started_at IS NULL",
                    (_DT, alloc.id),
                )
                task = asyncio.create_task(_do_stamp())
                await wait_until_backend_waiting(writer, stamper.info.backend_pid)
                assert not task.done()  # blocked on the writer's row lock, did not skip
            result = await asyncio.wait_for(task, timeout=5.0)
            assert result.active_started_at == _DT  # committed start observed after unblock
            assert result.active_ended_at == ended
            async with _conn(migrated_url) as check:
                db_started, db_ended = await _db_active_interval(check, alloc.id)
            assert db_started == _DT
            assert db_ended == ended
        finally:
            await writer.close()
            await stamper.close()

    asyncio.run(_run())


def test_stamp_active_ended_never_active_is_noop(migrated_url: str) -> None:
    # Release from granted with the start genuinely null (DB and snapshot agree): no stamp,
    # the interval stays open so reconcile takes the full credit (ADR-0007 §3).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(
                conn, res.id, state=AllocationState.GRANTED, active_started_at=None
            )
            result = await accounting.stamp_active_ended(conn, alloc, _DT + timedelta(hours=2))
            db_started, db_ended = await _db_active_interval(conn, alloc.id)
            assert db_ended is None
            assert db_started is None
            assert result.active_ended_at is None
            assert result.active_started_at is None

    asyncio.run(_run())


# --- usage(project) --------------------------------------------------------


def test_usage_reports_spent_remaining_and_by_cost_class(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn, limit="100")
            res = await _seed_resource(conn)
            alloc = await _seed_alloc(
                conn, res.id, active_started_at=_DT, active_ended_at=_DT + timedelta(hours=2)
            )
            await accounting.reserve(conn, alloc, Decimal("9.0000"))
            await accounting.reconcile(conn, alloc)
            usage = await accounting.usage(conn, "proj")
            assert usage.spent_kcu == Decimal("6.0000")
            assert usage.budget_remaining == Decimal("94.0000")
            assert usage.by_cost_class == {"local": Decimal("6.0000")}
            assert usage.shared_kcu == Decimal("0")

    asyncio.run(_run())


def test_usage_no_budget_row_denied_as_zero_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            usage = await accounting.usage(conn, "no-budget")
            # No budget row → read as limit 0, spent 0 (fail-closed, ADR-0007 §4).
            assert usage.spent_kcu == Decimal("0")
            assert usage.budget_remaining == Decimal("0")
            assert usage.by_cost_class == {}

    asyncio.run(_run())


# --- usage_for_investigation ----------------------------------------------


def test_usage_for_investigation_sums_owned_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            inv = await _seed_investigation(conn)
            for _ in range(2):
                alloc = await _seed_alloc(
                    conn, res.id, active_started_at=_DT, active_ended_at=_DT + timedelta(hours=1)
                )
                await accounting.reserve(conn, alloc, Decimal("3.0000"))
                await _seed_run_chain(conn, alloc, inv.id)
            total = await accounting.usage_for_investigation(conn, inv.id)
            assert total == Decimal("6.0000")  # 2 allocations × 3.0 reserved

    asyncio.run(_run())


def test_usage_for_investigation_excludes_shared_allocation(migrated_url: str) -> None:
    # An allocation whose Runs span two investigations is in neither per-investigation
    # sum (it is the project's shared_kcu) — no double-count (ADR-0007 §3).
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            inv_a = await _seed_investigation(conn)
            inv_b = await _seed_investigation(conn)
            # Owned-by-A allocation.
            owned = await _seed_alloc(conn, res.id)
            await accounting.reserve(conn, owned, Decimal("3.0000"))
            await _seed_run_chain(conn, owned, inv_a.id)
            # Shared allocation: one System with Runs in both A and B.
            shared = await _seed_alloc(conn, res.id)
            await accounting.reserve(conn, shared, Decimal("5.0000"))
            system = await SYSTEMS.insert(
                conn,
                System(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="alice",
                    project="proj",
                    allocation_id=shared.id,
                    state=SystemState.READY,
                    provisioning_profile={},
                ),
            )
            for inv_id in (inv_a.id, inv_b.id):
                await RUNS.insert(
                    conn,
                    Run(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="alice",
                        project="proj",
                        investigation_id=inv_id,
                        system_id=system.id,
                        state=RunState.CREATED,
                        build_profile={},
                    ),
                )
            total_a = await accounting.usage_for_investigation(conn, inv_a.id)
            total_b = await accounting.usage_for_investigation(conn, inv_b.id)
            assert total_a == Decimal("3.0000")  # only the owned allocation
            assert total_b == Decimal("0")  # B only ever saw the shared allocation
            usage = await accounting.usage(conn, "proj")
            assert usage.shared_kcu == Decimal("5.0000")  # shared shows only in project rollup

    asyncio.run(_run())


def test_shared_kcu_sums_in_project_total(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn)
            res = await _seed_resource(conn)
            inv_a = await _seed_investigation(conn)
            inv_b = await _seed_investigation(conn)
            shared = await _seed_alloc(conn, res.id)
            await accounting.reserve(conn, shared, Decimal("5.0000"))
            system = await SYSTEMS.insert(
                conn,
                System(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="alice",
                    project="proj",
                    allocation_id=shared.id,
                    state=SystemState.READY,
                    provisioning_profile={},
                ),
            )
            for inv_id in (inv_a.id, inv_b.id):
                await RUNS.insert(
                    conn,
                    Run(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="alice",
                        project="proj",
                        investigation_id=inv_id,
                        system_id=system.id,
                        state=RunState.CREATED,
                        build_profile={},
                    ),
                )
            usage = await accounting.usage(conn, "proj")
            # spent_kcu (running total) includes the shared reservation; shared_kcu isolates it.
            assert usage.spent_kcu == Decimal("5.0000")
            assert usage.shared_kcu == Decimal("5.0000")

    asyncio.run(_run())
