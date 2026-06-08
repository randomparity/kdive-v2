"""Release-time reconciliation wired into allocations.release (ADR-0007 §3, ADR-0040 §4).

A metered allocation (one with a budget row and a reserved ledger row) is reconciled when
released: release stamps active_ended_at on the active->releasing edge, then writes the
reconciled credit under PROJECT->ALLOCATION so it nets to rate*active_hours and leaves
spent_kcu == ledger Σ. Release on a terminal allocation is a stale_handle (already
reconciled). Double-release writes exactly one reconciled row.
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

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, RESOURCES
from kdive.domain.models import Allocation, Budget, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle import allocations as alloc_tools
from kdive.security.authz.rbac import Role
from kdive.services import accounting
from tests.db_waits import wait_until_any_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(role: Role = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=projects, roles={"proj": role}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_resource(pool: AsyncConnectionPool) -> UUID:
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
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
    return res.id


async def _seed_metered_alloc(
    pool: AsyncConnectionPool,
    resource_id: UUID,
    *,
    state: AllocationState,
    active_started_at: datetime | None,
    estimate: str,
) -> UUID:
    """Seed a budget, an allocation in ``state``, and its reserved ledger row."""
    async with pool.connection() as conn:
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
                principal="user-1",
                project="proj",
                resource_id=resource_id,
                state=state,
                requested_vcpus=2,
                requested_memory_gb=4,
                active_started_at=active_started_at,
            ),
        )
        await accounting.reserve(conn, alloc, Decimal(estimate))
    return alloc.id


async def _spent(pool: AsyncConnectionPool) -> Decimal:
    async with pool.connection() as conn:
        budget = await BUDGETS.get(conn, "proj")
    assert budget is not None
    return budget.spent_kcu


async def _ledger_rows(pool: AsyncConnectionPool, alloc_id: UUID) -> list[tuple[str, Decimal]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT event_type, kcu_delta FROM ledger WHERE allocation_id = %s ORDER BY ts",
            (alloc_id,),
        )
        rows = await cur.fetchall()
    return [(r[0], Decimal(r[1])) for r in rows]


def test_release_active_allocation_reconciles_to_actual(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            started = datetime.now(UTC) - timedelta(hours=2)
            # rate = 3.0 kcu/hr; reserve a 3h window = 9.0; ~2h active.
            alloc_id = await _seed_metered_alloc(
                pool,
                res_id,
                state=AllocationState.ACTIVE,
                active_started_at=started,
                estimate="9.0000",
            )
            resp = await alloc_tools.release_allocation(pool, _ctx(), str(alloc_id))
            assert resp.status == "released"
            rows = await _ledger_rows(pool, alloc_id)
            assert [r[0] for r in rows] == ["reserved", "reconciled"]
            # ~2h active -> actual ~6.0; net spent ~6.0; spent == ledger Σ.
            net = sum((r[1] for r in rows), Decimal(0))
            assert await _spent(pool) == net
            assert Decimal("5.9") < net < Decimal("6.1")
            # active_ended_at was stamped.
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, alloc_id)
            assert alloc is not None and alloc.active_ended_at is not None

    asyncio.run(_run())


def test_release_from_granted_is_full_credit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id = await _seed_metered_alloc(
                pool,
                res_id,
                state=AllocationState.GRANTED,
                active_started_at=None,  # never went active
                estimate="9.0000",
            )
            resp = await alloc_tools.release_allocation(pool, _ctx(), str(alloc_id))
            assert resp.status == "released"
            rows = await _ledger_rows(pool, alloc_id)
            assert rows == [("reserved", Decimal("9.0000")), ("reconciled", Decimal("-9.0000"))]
            assert await _spent(pool) == Decimal("0.0000")  # full credit

    asyncio.run(_run())


def test_double_release_writes_one_reconciled_row(migrated_url: str) -> None:
    # Re-releasing a released allocation is a stale_handle and writes no second credit.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id = await _seed_metered_alloc(
                pool,
                res_id,
                state=AllocationState.GRANTED,
                active_started_at=None,
                estimate="9.0000",
            )
            first = await alloc_tools.release_allocation(pool, _ctx(), str(alloc_id))
            assert first.status == "released"
            second = await alloc_tools.release_allocation(pool, _ctx(), str(alloc_id))
            assert second.status == "error"
            assert second.error_category == "stale_handle"
            reconciled = [r for r in await _ledger_rows(pool, alloc_id) if r[0] == "reconciled"]
            assert len(reconciled) == 1  # exactly one credit despite two releases
            assert await _spent(pool) == Decimal("0.0000")

    asyncio.run(_run())


def test_concurrent_release_vs_expired_sweep_reconciles_once(migrated_url: str) -> None:
    # The ->expired sweep (⑤) and release both take PROJECT->ALLOCATION and reconcile;
    # whichever reaches the allocation lock first wins, the other sees a terminal state
    # and skips. Simulate the sweep by pre-holding the locks and flipping the allocation
    # to expired + reconciling on connection A while release blocks on connection B.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id = await _seed_metered_alloc(
                pool,
                res_id,
                state=AllocationState.GRANTED,
                active_started_at=None,
                estimate="9.0000",
            )
            sweep = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
            try:
                async with (
                    sweep.transaction(),
                    advisory_xact_lock(sweep, LockScope.PROJECT, "proj"),
                    advisory_xact_lock(sweep, LockScope.ALLOCATION, alloc_id),
                ):
                    # release on the pool blocks behind the held PROJECT lock.
                    task = asyncio.ensure_future(
                        alloc_tools.release_allocation(pool, _ctx(), str(alloc_id))
                    )
                    await wait_until_any_backend_waiting(sweep, locktype="advisory")
                    assert not task.done()  # blocked on the sweep's locks
                    # The sweep flips ->expired and reconciles, then releases the locks.
                    alloc = await ALLOCATIONS.update_state(sweep, alloc_id, AllocationState.EXPIRED)
                    await accounting.reconcile(sweep, alloc)
                resp = await task
            finally:
                await sweep.close()
            # Release lost the race: the allocation is terminal -> stale_handle, no 2nd credit.
            assert resp.status == "error"
            assert resp.error_category == "stale_handle"
            reconciled = [r for r in await _ledger_rows(pool, alloc_id) if r[0] == "reconciled"]
            assert len(reconciled) == 1
            assert await _spent(pool) == Decimal("0.0000")

    asyncio.run(_run())


def test_release_active_without_size_fails_clean_and_rolls_back(migrated_url: str) -> None:
    # An active allocation that cannot be priced (null persisted size) must return a typed
    # failure, not crash the handler, and must leave no terminal transition or ledger row
    # (the locked transaction rolled back).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            async with pool.connection() as conn:
                await BUDGETS.upsert(
                    conn,
                    Budget(
                        project="proj",
                        limit_kcu=Decimal("1000"),
                        spent_kcu=Decimal(0),
                        updated_at=_DT,
                    ),
                )
                alloc = await ALLOCATIONS.insert(
                    conn,
                    Allocation(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="user-1",
                        project="proj",
                        resource_id=res_id,
                        state=AllocationState.ACTIVE,
                        requested_vcpus=None,  # cannot price actual
                        requested_memory_gb=None,
                        active_started_at=_DT,
                    ),
                )
                await accounting.reserve(conn, alloc, Decimal("9.0000"))
            resp = await alloc_tools.release_allocation(pool, _ctx(), str(alloc.id))
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, alloc.id)
            assert latest is not None and latest.state is AllocationState.ACTIVE  # rolled back
            reconciled = [r for r in await _ledger_rows(pool, alloc.id) if r[0] == "reconciled"]
            assert reconciled == []  # no credit written
            assert await _spent(pool) == Decimal("9.0000")  # spent unchanged

    asyncio.run(_run())
