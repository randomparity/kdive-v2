"""Pending-queue admission tests — on_capacity=queue enqueue path (ADR-0069, #164).

A capacity-denied request with on_capacity=queue rests as a `requested` allocation holding
only a queue position: no budget reserve, no lease, no occupancy slot, resource_id NULL, the
original request inputs persisted. Budget and configuration denials still hard-deny. Real
Postgres; admission driven directly with injected contexts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeClaim, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.services.allocation.admission import AllocationRequest, admit
from tests.db_waits import wait_until_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=1, memory_gb=0, cost_class="local")
_X710 = PCIeDescriptor(
    bdf="0000:3b:00.0",
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


def _queued_request(resource: Resource) -> AllocationRequest:
    return AllocationRequest(
        ctx=CTX,
        resource=resource,
        project="proj",
        selector=SEL,
        window=1,
        on_capacity="queue",
        disk_gb=10,
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
    )


async def _seed_resource(
    conn: psycopg.AsyncConnection, *, cap: int, pcie: list[PCIeDescriptor] | None = None
) -> Resource:
    capabilities: dict[str, object] = {
        CONCURRENT_ALLOCATION_CAP_KEY: cap,
        "vcpus": 64,
        "memory_mb": 65536,
    }
    if pcie is not None:
        capabilities[PCIE_DEVICES_KEY] = pcie
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities=capabilities,
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_quota(
    conn: psycopg.AsyncConnection,
    *,
    limit: str = "1000000",
    allocs: int = 1_000_000,
    pending: int = 10,
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
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    *,
    pcie_claim: list[PCIeClaim] | None = None,
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
            pcie_claim=pcie_claim or [],
        ),
    )


async def _scalar(conn: psycopg.AsyncConnection, query: LiteralString, *params: object) -> int:
    async with conn.cursor() as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_ledger(conn: psycopg.AsyncConnection) -> int:
    return await _scalar(conn, "SELECT count(*) FROM ledger")


def test_host_cap_denial_with_queue_enqueues_a_requested_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_quota(conn)
            await _seed_granted(conn, res.id)  # fills the single host slot
            outcome = await admit(conn, _queued_request(res))
            assert outcome.granted is True
            alloc = outcome.allocation
            assert alloc is not None
            assert alloc.state is AllocationState.REQUESTED
            assert alloc.resource_id is None  # holds no host
            assert alloc.lease_expiry is None  # no lease
            assert alloc.pcie_claim == []  # no device resolved at enqueue
            # Request inputs persisted to re-admit (#165).
            assert alloc.requested_vcpus == 1
            assert alloc.requested_disk_gb == 10
            assert alloc.requested_kind is ResourceKind.LOCAL_LIBVIRT
            # No reserve: the ledger holds nothing for a queued row (only the seeded grant
            # was inserted directly, never via accounting, so the ledger is empty).
            assert await _count_ledger(conn) == 0
            # spent_kcu unchanged (no reserve).
            assert await _scalar(conn, "SELECT spent_kcu FROM budgets WHERE project='proj'") == 0

    asyncio.run(_run())


def test_grant_quota_denial_with_queue_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=10)
            await _seed_quota(conn, allocs=1)
            await _seed_granted(conn, res.id)  # occupies the single grant-quota slot
            outcome = await admit(conn, _queued_request(res))
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.state is AllocationState.REQUESTED

    asyncio.run(_run())


def test_pcie_capacity_denial_with_queue_enqueues(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=10, pcie=[_X710])
            await _seed_quota(conn)
            await _seed_granted(
                conn,
                res.id,
                pcie_claim=[PCIeClaim(bdf="0000:3b:00.0", vendor_id="8086", device_id="1572")],
            )
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    on_capacity="queue",
                    disk_gb=10,
                    requested_kind=ResourceKind.LOCAL_LIBVIRT,
                    pcie_specs=("8086:1572",),
                ),
            )
            assert outcome.granted is True
            alloc = outcome.allocation
            assert alloc is not None
            assert alloc.state is AllocationState.REQUESTED
            assert alloc.resource_id is None
            assert alloc.pcie_claim == []
            assert await _count_ledger(conn) == 0

    asyncio.run(_run())


def test_default_deny_path_unchanged(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_quota(conn)
            await _seed_granted(conn, res.id)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    on_capacity="deny",
                    disk_gb=10,
                    requested_kind=ResourceKind.LOCAL_LIBVIRT,
                ),
            )
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert outcome.reason == "at_capacity"
            # No queued row written.
            assert (
                await _scalar(conn, "SELECT count(*) FROM allocations WHERE state='requested'") == 0
            )

    asyncio.run(_run())


def test_budget_denial_with_queue_still_hard_denies(migrated_url: str) -> None:
    # A budget denial shares allocation_denied with the host-cap denial but is NOT queueable.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=10)
            await _seed_quota(conn, limit="0")  # zero budget → budget denial
            outcome = await admit(conn, _queued_request(res))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _scalar(conn, "SELECT count(*) FROM allocations") == 0

    asyncio.run(_run())


def test_configuration_denial_with_queue_still_hard_denies(migrated_url: str) -> None:
    # An invalid host cap is a configuration_error — never queueable.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=-1)  # invalid cap → configuration_error
            await _seed_quota(conn)
            outcome = await admit(conn, _queued_request(res))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _scalar(conn, "SELECT count(*) FROM allocations") == 0

    asyncio.run(_run())


def test_pending_cap_full_denies_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_quota(conn, pending=1)
            await _seed_granted(conn, res.id)
            first = await admit(conn, _queued_request(res))
            assert first.granted is True  # first enqueue uses the single pending slot
            second = await admit(conn, _queued_request(res))
            assert second.granted is False
            assert second.category is ErrorCategory.QUOTA_EXCEEDED
            assert (
                await _scalar(conn, "SELECT count(*) FROM allocations WHERE state='requested'") == 1
            )

    asyncio.run(_run())


def test_zero_pending_cap_denies_enqueue(migrated_url: str) -> None:
    # The migration backfills max_pending_allocations to 0 — the queue is opt-out by default.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_quota(conn, pending=0)
            await _seed_granted(conn, res.id)
            outcome = await admit(conn, _queued_request(res))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            assert (
                await _scalar(conn, "SELECT count(*) FROM allocations WHERE state='requested'") == 0
            )

    asyncio.run(_run())


def test_idempotent_enqueue_returns_the_same_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_quota(conn)
            await _seed_granted(conn, res.id)
            first = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    idempotency_key="k1",
                    on_capacity="queue",
                    disk_gb=10,
                    requested_kind=ResourceKind.LOCAL_LIBVIRT,
                ),
            )
            second = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    idempotency_key="k1",
                    on_capacity="queue",
                    disk_gb=10,
                    requested_kind=ResourceKind.LOCAL_LIBVIRT,
                ),
            )
            assert first.allocation is not None and second.allocation is not None
            assert first.allocation.id == second.allocation.id  # same queued row
            assert (
                await _scalar(conn, "SELECT count(*) FROM allocations WHERE state='requested'") == 1
            )

    asyncio.run(_run())


def test_concurrent_enqueue_past_cap_serializes_on_project_lock(migrated_url: str) -> None:
    # Two concurrent on_capacity=queue requests with pending cap 1: the PROJECT lock
    # serializes the count-then-insert so exactly one enqueues and one is denied.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as seed,
            _conn(migrated_url) as a,
            _conn(migrated_url) as b,
        ):
            res = await _seed_resource(seed, cap=1)
            await _seed_quota(seed, pending=1)
            await _seed_granted(seed, res.id)
            async with a.transaction(), advisory_xact_lock(a, LockScope.PROJECT, "proj"):
                task = asyncio.ensure_future(admit(b, _queued_request(res)))
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()  # B blocked on the PROJECT lock A holds
                first = await admit(a, _queued_request(res))  # A enqueues under its own lock
            second = await task  # B proceeds after A releases
            granted = [o for o in (first, second) if o.granted]
            denied = [o for o in (first, second) if not o.granted]
            assert len(granted) == 1 and len(denied) == 1
            assert denied[0].category is ErrorCategory.QUOTA_EXCEEDED
            assert (
                await _scalar(seed, "SELECT count(*) FROM allocations WHERE state='requested'") == 1
            )

    asyncio.run(_run())


@pytest.mark.parametrize("on_capacity", ["deny", "queue"])
def test_grant_path_unaffected_when_under_cap(
    migrated_url: str, on_capacity: Literal["deny", "queue"]
) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2)
            await _seed_quota(conn)
            outcome = await admit(
                conn,
                AllocationRequest(
                    ctx=CTX,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=1,
                    on_capacity=on_capacity,
                    disk_gb=10,
                    requested_kind=ResourceKind.LOCAL_LIBVIRT,
                ),
            )
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.state is AllocationState.GRANTED
            assert outcome.allocation.resource_id == res.id

    asyncio.run(_run())
