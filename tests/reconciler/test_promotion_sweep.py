"""The reconciler work-conserving FIFO promotion sweep + queue_timeout reaper (ADR-0069).

The sweep re-runs selection (PCIe-aware, cordon-skipping) from each queued `requested`
allocation's persisted inputs and promotes the oldest *placeable* request per resource to
`granted` — stamping `resource_id`, reserving at grant, and setting the lease — under
PROJECT -> RESOURCE -> ALLOCATION (sharing admission's RESOURCE lock and the expiry sweep's
ALLOCATION fence). A budget recheck failure at promotion terminates the request (`failed`),
not re-queue; a request never placeable past the max-wait window is reaped to
`failed(queue_timeout)`. Real Postgres; the sweep is driven directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.reconciler import loop
from kdive.security.audit import args_digest
from kdive.services.allocation_admission import AllocationRequest, admit
from tests.db_waits import wait_until_any_backend_waiting
from tests.reconciler.conftest import connect, run_repair

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_KIND = "local-libvirt"
_NIC = {
    "bdf": "0000:01:00.0",
    "vendor_id": "8086",
    "device_id": "1572",
    "class_code": "0200",
    "label": "x710",
}


async def _seed_resource(
    conn: psycopg.AsyncConnection,
    *,
    cap: int = 1,
    cordoned: bool = False,
    status: ResourceStatus = ResourceStatus.AVAILABLE,
    pcie: bool = False,
) -> Resource:
    caps: dict[str, object] = {CONCURRENT_ALLOCATION_CAP_KEY: cap, "vcpus": 64, "memory_mb": 65536}
    if pcie:
        caps[PCIE_DEVICES_KEY] = [_NIC]
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities=caps,
            pool="local-libvirt",
            cost_class="local",
            status=status,
            host_uri="qemu:///system",
            cordoned=cordoned,
        ),
    )


async def _seed_quota(
    conn: psycopg.AsyncConnection,
    *,
    limit: str = "1000000",
    allocs: int = 1_000_000,
    pending: int = 100,
) -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
    )
    await QUOTAS.upsert(
        conn,
        Quota(
            project="proj",
            max_concurrent_allocations=allocs,
            max_concurrent_systems=1_000_000,
            max_pending_allocations=pending,
            updated_at=_DT,
        ),
    )


async def _seed_granted(
    conn: psycopg.AsyncConnection, resource_id: UUID, *, pcie: list[PCIeClaim] | None = None
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
            state=AllocationState.GRANTED,
            pcie_claim=pcie or [],
        ),
    )


async def _enqueue(
    conn: psycopg.AsyncConnection,
    resource: Resource,
    *,
    principal: str = "alice",
    agent_session: str | None = "sess-1",
    by_id: UUID | None = None,
    pcie_specs: tuple[str, ...] = (),
    created_offset: timedelta = timedelta(0),
) -> UUID:
    """Enqueue a queued `requested` row via admit (on_capacity=queue) against a full host.

    The host must be at capacity so admit enqueues rather than grants. ``created_offset``
    back/forward-dates `created_at` so FIFO + max-wait can be exercised deterministically.
    """
    ctx = RequestContext(principal=principal, agent_session=agent_session, projects=("proj",))
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
            pcie_specs=pcie_specs,
            requested_kind=None if by_id is not None else _KIND,
            requested_resource_id=by_id,
        ),
    )
    assert outcome.granted and outcome.allocation is not None
    alloc = outcome.allocation
    assert alloc.state is AllocationState.REQUESTED
    if created_offset != timedelta(0):
        await conn.execute(
            "UPDATE allocations SET created_at = now() + %s WHERE id = %s",
            (created_offset, alloc.id),
        )
    return alloc.id


async def _state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _row(conn: psycopg.AsyncConnection, alloc_id: UUID) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


async def _ledger_kinds(conn: psycopg.AsyncConnection, alloc_id: UUID) -> list[str]:
    cur = await conn.execute(
        "SELECT event_type FROM ledger WHERE allocation_id = %s ORDER BY ts, id", (alloc_id,)
    )
    return [r[0] for r in await cur.fetchall()]


def test_freed_slot_promotes_and_charges_at_grant(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)  # fills the only slot
            queued = await _enqueue(seed, res)
            # Free the slot so the sweep can place the queued request.
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 1
        async with await connect(migrated_url) as check:
            row = await _row(check, queued)
            assert row["state"] == "granted"
            assert row["resource_id"] == res.id  # stamped at promotion
            assert row["lease_expiry"] is not None  # lease window set
            assert await _ledger_kinds(check, queued) == ["reserved"]  # charged at grant
            spent = await check.execute("SELECT spent_kcu FROM budgets WHERE project='proj'")
            spent_row = await spent.fetchone()
            assert spent_row is not None and Decimal(spent_row[0]) > 0

    asyncio.run(_run())


def test_work_conserving_fills_free_host_behind_busy_global_oldest(migrated_url: str) -> None:
    # The global-oldest request targets host A (busy); a younger request placeable on free
    # host B is promoted first — a free host is never idled behind a request on a busy host.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_a = await _seed_resource(seed, cap=1)
            host_b = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            hold_a = await _seed_granted(seed, host_a.id)  # A full
            hold_b = await _seed_granted(seed, host_b.id)  # B full (so both enqueue)
            oldest = await _enqueue(
                seed, host_a, by_id=host_a.id, created_offset=timedelta(hours=-2)
            )
            younger = await _enqueue(
                seed, host_b, by_id=host_b.id, created_offset=timedelta(hours=-1)
            )
            # Free only B; A stays busy. The global-oldest (A) must not idle free B.
            await ALLOCATIONS.update_state(seed, hold_b.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, hold_b.id, AllocationState.RELEASED)
            assert hold_a.state is AllocationState.GRANTED  # A still occupied
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _state(check, oldest) == "requested"  # still waits on busy A
            assert await _state(check, younger) == "granted"  # placed on free B

    asyncio.run(_run())


def test_over_budget_at_promotion_terminates_not_requeue(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
            # Drain the budget AFTER enqueue so promotion's budget recheck fails.
            await seed.execute("UPDATE budgets SET limit_kcu = 0 WHERE project='proj'")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
            # A second pass must NOT retry the now-failed row (no infinite retry).
            again = await run_repair(pool, loop._promote_pending)
        assert count == 0  # nothing promoted
        assert again == 0
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "failed"  # terminated, not re-queued
            assert await _ledger_kinds(check, queued) == []  # never reserved

    asyncio.run(_run())


def test_host_full_and_over_budget_terminates_on_budget(migrated_url: str) -> None:
    # A request that is BOTH host-cap-full AND over budget is TERMINATED (budget), not parked
    # — proving terminate-vs-wait branches on the queueable flag, not the shared
    # ALLOCATION_DENIED category the budget and host-cap denials share.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            await _seed_granted(seed, res.id)  # host stays full (slot never freed)
            queued = await _enqueue(seed, res)
            await seed.execute("UPDATE budgets SET limit_kcu = 0 WHERE project='proj'")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "failed"  # budget terminates over host-wait

    asyncio.run(_run())


def test_grant_quota_headroom_one_promotes_exactly_one(migrated_url: str) -> None:
    # Per-project grant quota headroom = 1, two placeable queued rows on two free hosts ->
    # exactly one promoted; the second stays requested (per-candidate committed transaction
    # re-reads the post-promotion occupancy count, so the quota is never overshot).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_a = await _seed_resource(seed, cap=1)
            host_b = await _seed_resource(seed, cap=1)
            host_q = await _seed_resource(seed, cap=1)
            await _seed_quota(seed, allocs=1)  # one grant slot for the whole project
            quota_holder = await _seed_granted(seed, host_q.id)  # consumes the only grant slot
            first = await _enqueue(
                seed, host_a, by_id=host_a.id, created_offset=timedelta(hours=-2)
            )
            second = await _enqueue(
                seed, host_b, by_id=host_b.id, created_offset=timedelta(hours=-1)
            )
            # Free the grant slot: headroom becomes 1 with two placeable queued rows.
            await ALLOCATIONS.update_state(seed, quota_holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, quota_holder.id, AllocationState.RELEASED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 1
        async with await connect(migrated_url) as check:
            states = sorted([await _state(check, first), await _state(check, second)])
            assert states == ["granted", "requested"]

    asyncio.run(_run())


def test_cordoned_host_skipped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res, by_id=res.id)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
            # Cordon the (now-free) host: placement must skip it.
            await seed.execute("UPDATE resources SET cordoned = true WHERE id = %s", (res.id,))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "requested"  # cordoned host not used

    asyncio.run(_run())


def test_pcie_aware_promotion_claims_a_freed_device(migrated_url: str) -> None:
    # A PCIe request waits while the device is busy, then is promoted (claiming the device)
    # once it frees. PCIe-busy is a WAIT, not a terminate.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=2, pcie=True)
            await _seed_quota(seed)
            nic_holder = await _seed_granted(
                seed,
                res.id,
                pcie=[
                    PCIeClaim(
                        bdf=_NIC["bdf"], vendor_id=_NIC["vendor_id"], device_id=_NIC["device_id"]
                    )
                ],
            )
            slot_holder = await _seed_granted(seed, res.id)  # fills the cap so enqueue happens
            queued = await _enqueue(seed, res, pcie_specs=("8086:1572",))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            # Free a slot but keep the NIC busy -> PCIe-busy is a WAIT, not a failure.
            async with await connect(migrated_url) as free:
                await ALLOCATIONS.update_state(free, slot_holder.id, AllocationState.RELEASING)
                await ALLOCATIONS.update_state(free, slot_holder.id, AllocationState.RELEASED)
            first = await run_repair(pool, loop._promote_pending)
            async with await connect(migrated_url) as free:
                await ALLOCATIONS.update_state(free, nic_holder.id, AllocationState.RELEASING)
                await ALLOCATIONS.update_state(free, nic_holder.id, AllocationState.RELEASED)
            second = await run_repair(pool, loop._promote_pending)
        assert first == 0  # NIC busy -> stayed requested
        assert second == 1  # NIC freed -> promoted
        async with await connect(migrated_url) as check:
            row = await _row(check, queued)
            assert row["state"] == "granted"
            claimed = row["pcie_claim"]
            assert isinstance(claimed, list) and len(claimed) == 1
            assert claimed[0]["bdf"] == _NIC["bdf"]  # device claimed at grant
            assert await _state(check, queued) == "granted"

    asyncio.run(_run())


def test_pcie_promotion_logs_malformed_persisted_spec(
    migrated_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> tuple[UUID, UUID]:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1, pcie=True)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res)
            await seed.execute(
                "UPDATE allocations SET requested_pcie_specs = %s WHERE id = %s",
                (Jsonb(["not-a-spec"]), queued),
            )
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
        caplog.set_level(logging.WARNING, logger="kdive.services.allocation_promotion")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._promote_pending)
        assert count == 0
        return queued, res.id

    queued, resource_id = asyncio.run(_run())
    assert any(
        record.exc_info is not None
        and "configuration_error" in record.message
        and str(queued) in record.message
        and str(resource_id) in record.message
        for record in caplog.records
    )


def test_grant_audit_attributed_to_original_principal(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res, principal="bob", agent_session="bob-sess")
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._promote_pending)
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT principal, agent_session FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->granted'",
                (queued,),
            )
            row = await cur.fetchone()
            assert row == ("bob", "bob-sess")  # original attribution, not the service identity

    asyncio.run(_run())


def test_released_while_queued_is_not_promoted(migrated_url: str) -> None:
    # A release that pre-holds PROJECT -> ALLOCATION on the queued row blocks the sweep; the
    # row goes released; the sweep then re-reads under the lock and skips it (no promote of a
    # released row). Proves lock sharing fences the release-vs-promote race.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
        blocker = await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)
        try:
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                async with (
                    blocker.transaction(),
                    advisory_xact_lock(blocker, LockScope.PROJECT, "proj"),
                    advisory_xact_lock(blocker, LockScope.ALLOCATION, queued),
                ):
                    task = asyncio.ensure_future(run_repair(pool, loop._promote_pending))
                    await wait_until_any_backend_waiting(blocker, locktype="advisory")
                    assert not task.done()  # blocked on the locks the release holds
                    # Cancel the queued row while the sweep waits.
                    await ALLOCATIONS.update_state(blocker, queued, AllocationState.RELEASED)
                count = await task
        finally:
            await blocker.close()
        assert count == 0  # the sweep saw a terminal row and skipped it
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "released"  # not promoted

    asyncio.run(_run())


def test_never_placeable_past_max_wait_failed_queue_timeout(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            await _seed_granted(seed, res.id)  # host stays full forever
            queued = await _enqueue(seed, res, created_offset=timedelta(hours=-48))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            reaped = await run_repair(
                pool, lambda conn: loop._reap_queue_timeouts(conn, timedelta(hours=24))
            )
        assert reaped == 1
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "failed"
            cur = await check.execute(
                "SELECT tool, args_digest FROM audit_log WHERE object_id = %s "
                "AND transition = 'requested->failed'",
                (queued,),
            )
            row = await cur.fetchone()
            assert row is not None  # the failed transition was audited
            assert row[0] == "reconciler.reap_queue_timeout"  # reaped, not a budget terminate
            # The category recorded is queue_timeout, NOT lease_expired — the args carry the
            # queue_timeout reason, whose digest differs from a lease_expired digest.
            assert row[1] == args_digest(
                {"reason": ErrorCategory.QUEUE_TIMEOUT.value, "project": "proj"}
            )
            assert row[1] != args_digest(
                {"reason": ErrorCategory.LEASE_EXPIRED.value, "project": "proj"}
            )

    asyncio.run(_run())


def test_fresh_queued_row_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            await _seed_granted(seed, res.id)
            queued = await _enqueue(seed, res)  # just enqueued, well within the window
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            reaped = await run_repair(
                pool, lambda conn: loop._reap_queue_timeouts(conn, timedelta(hours=24))
            )
        assert reaped == 0
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "requested"  # young row untouched

    asyncio.run(_run())


def test_aged_but_placeable_is_promoted_not_reaped(migrated_url: str) -> None:
    # A request past the max-wait window that IS placeable this pass is promoted by the
    # promotion step (which runs before the reaper in reconcile_once), not reaped.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed)
            holder = await _seed_granted(seed, res.id)  # full at enqueue
            queued = await _enqueue(seed, res, created_offset=timedelta(hours=-48))
            # Free the host so the aged row is placeable on this pass.
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(seed, holder.id, AllocationState.RELEASED)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await loop.reconcile_once(
                pool, loop.NullReaper(), queue_max_wait=timedelta(hours=24)
            )
        assert report.promoted_allocations == 1
        assert report.queue_timeouts == 0
        async with await connect(migrated_url) as check:
            assert await _state(check, queued) == "granted"  # placed, not reaped

    asyncio.run(_run())
