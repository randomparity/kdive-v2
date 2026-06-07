"""Lease renewal wired into allocations.renew (ADR-0036 §3, ADR-0040 §3).

renew extends a non-terminal allocation's lease_expiry by the clamped added window and
writes an incremental reserved row + spent_kcu bump under the PROJECT lock. A replayed
idempotency_key neither re-extends nor re-charges; over budget denies and leaves the
window unchanged; a terminal allocation is a stale_handle. Handlers are tested directly
with injected pool + RequestContext (no MCP transport).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, RESOURCES
from kdive.domain.models import Allocation, Budget, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle import allocations as alloc_tools
from kdive.security.rbac import AuthorizationError, Role
from kdive.services import accounting
from kdive.services.allocation_renew import _RENEW_KIND

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role = Role.OPERATOR,
    *,
    principal: str = "user-1",
    projects: tuple[str, ...] = ("proj",),
) -> RequestContext:
    return RequestContext(
        principal=principal, agent_session="s", projects=projects, roles={"proj": role}
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
    state: AllocationState = AllocationState.GRANTED,
    lease_hours_from_now: float = 2.0,
    limit_kcu: str = "1000",
    estimate: str = "9.0000",
) -> tuple[UUID, datetime]:
    """Seed a budget, an allocation with a lease and reserved row; return (id, lease_expiry)."""
    lease_expiry = datetime.now(UTC) + timedelta(hours=lease_hours_from_now)
    async with pool.connection() as conn:
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal(limit_kcu),
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
                resource_id=resource_id,
                state=state,
                lease_expiry=lease_expiry,
                requested_vcpus=2,
                requested_memory_gb=4,
            ),
        )
        await accounting.reserve(conn, alloc, Decimal(estimate))
    return alloc.id, lease_expiry


async def _ledger_rows(pool: AsyncConnectionPool, alloc_id: UUID) -> list[tuple[str, Decimal]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT event_type, kcu_delta FROM ledger WHERE allocation_id = %s ORDER BY ts, id",
            (alloc_id,),
        )
        rows = await cur.fetchall()
    return [(r[0], Decimal(r[1])) for r in rows]


async def _alloc(pool: AsyncConnectionPool, alloc_id: UUID) -> Allocation:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, alloc_id)
    assert alloc is not None
    return alloc


async def _spent(pool: AsyncConnectionPool) -> Decimal:
    async with pool.connection() as conn:
        budget = await BUDGETS.get(conn, "proj")
    assert budget is not None
    return budget.spent_kcu


def test_renew_extends_lease_and_writes_incremental_reserved(migrated_url: str) -> None:
    # rate = 2*1.0 + 4*0.25 = 3.0 kcu/hr; +3h -> +9.0 reserved, lease pushed out 3h.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(pool, res_id, lease_hours_from_now=2.0)
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=3)
            assert resp.status == "granted"
            rows = await _ledger_rows(pool, alloc_id)
            assert [r[0] for r in rows] == ["reserved", "reserved"]
            assert rows[1][1] == Decimal("9.0000")
            assert await _spent(pool) == Decimal("18.0000")  # 9 grant + 9 renew
            after = (await _alloc(pool, alloc_id)).lease_expiry
            assert after is not None
            assert after - before == timedelta(hours=3)

    asyncio.run(_run())


def test_replayed_idempotency_key_neither_extends_nor_charges(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, _ = await _seed_metered_alloc(pool, res_id)
            first = await alloc_tools.renew_allocation(
                pool, _ctx(), str(alloc_id), extend=2, idempotency_key="k-1"
            )
            assert first.status == "granted"
            expiry_after_first = (await _alloc(pool, alloc_id)).lease_expiry
            spent_after_first = await _spent(pool)
            second = await alloc_tools.renew_allocation(
                pool, _ctx(), str(alloc_id), extend=2, idempotency_key="k-1"
            )
            assert second.status == "granted"  # replay returns the prior result
            assert (await _alloc(pool, alloc_id)).lease_expiry == expiry_after_first
            assert await _spent(pool) == spent_after_first  # no second charge
            reserved = [r for r in await _ledger_rows(pool, alloc_id) if r[0] == "reserved"]
            assert len(reserved) == 2  # grant + one renew, not two renews

    asyncio.run(_run())


def test_over_budget_renew_denies_window_unchanged(migrated_url: str) -> None:
    # budget limit just covers the 9.0 grant; a +3h renew (9.0 more) is over budget.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(
                pool, res_id, limit_kcu="9", estimate="9.0000"
            )
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=3)
            assert resp.status == "error"
            assert resp.error_category == "allocation_denied"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before  # unchanged
            assert await _spent(pool) == Decimal("9.0000")  # no extra charge
            reserved = [r for r in await _ledger_rows(pool, alloc_id) if r[0] == "reserved"]
            assert len(reserved) == 1

    asyncio.run(_run())


def test_renew_terminal_allocation_is_stale_handle(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(
                pool, res_id, state=AllocationState.RELEASED
            )
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=2)
            assert resp.status == "error"
            assert resp.error_category == "stale_handle"
            assert resp.data["current_status"] == "released"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before

    asyncio.run(_run())


def test_non_positive_extend_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(pool, res_id)
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=0)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before

    asyncio.run(_run())


def test_renew_at_cap_is_config_error_window_unchanged(migrated_url: str) -> None:
    # Lease already past the 24h KDIVE_LEASE_MAX ceiling: no billable extension. Seed 25h
    # out so the clamp yields zero even with wall-clock slack between seed and renew.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(pool, res_id, lease_hours_from_now=25.0)
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=5)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before
            assert await _spent(pool) == Decimal("9.0000")

    asyncio.run(_run())


def test_renew_clamps_added_window_to_cap(migrated_url: str) -> None:
    # Lease 20h out; +10h target = 30h, clamped to 24h -> bill 4h = 12.0 kcu.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, _ = await _seed_metered_alloc(pool, res_id, lease_hours_from_now=20.0)
            now = datetime.now(UTC)
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(alloc_id), extend=10)
            assert resp.status == "granted"
            after = (await _alloc(pool, alloc_id)).lease_expiry
            assert after is not None
            # New expiry ~24h from now (clamp), within a small wall-clock slack.
            assert timedelta(hours=23, minutes=59) < after - now < timedelta(hours=24, minutes=1)
            reserved = [r for r in await _ledger_rows(pool, alloc_id) if r[0] == "reserved"]
            assert reserved[1][1] == Decimal("12.0000")  # 4h * 3.0 kcu/hr

    asyncio.run(_run())


def test_renew_unknown_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await alloc_tools.renew_allocation(pool, _ctx(), str(uuid4()), extend=2)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_renew_malformed_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await alloc_tools.renew_allocation(pool, _ctx(), "not-a-uuid", extend=2)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_renew_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, _ = await _seed_metered_alloc(pool, res_id)
            try:
                await alloc_tools.renew_allocation(pool, _ctx(Role.VIEWER), str(alloc_id), extend=2)
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_key_reused_across_request_kind_is_rejected(migrated_url: str) -> None:
    # A (principal, key) already stored under the request kind cannot be reused for a
    # renew: the shared PK conflicts and renew fails closed (configuration_error), and the
    # lease/charge are unchanged (the txn rolled back).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, before = await _seed_metered_alloc(pool, res_id)
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("dup", "user-1", "proj", "allocations.request", '{"allocation_id": "x"}'),
                )
            resp = await alloc_tools.renew_allocation(
                pool, _ctx(), str(alloc_id), extend=2, idempotency_key="dup"
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before
            assert await _spent(pool) == Decimal("9.0000")

    asyncio.run(_run())


def test_idempotency_key_scoped_to_renew_kind(migrated_url: str) -> None:
    # A successful renew stores its key under the renew kind, not the request kind.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _seed_resource(pool)
            alloc_id, _ = await _seed_metered_alloc(pool, res_id)
            await alloc_tools.renew_allocation(
                pool, _ctx(), str(alloc_id), extend=2, idempotency_key="k-kind"
            )
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT kind FROM idempotency_keys WHERE principal = %s AND key = %s",
                    ("user-1", "k-kind"),
                )
                row = await cur.fetchone()
            assert row is not None and row[0] == _RENEW_KIND

    asyncio.run(_run())
