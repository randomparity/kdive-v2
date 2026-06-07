"""Tests for the per-host capacity check inside M1 admission (ADR-0023, ADR-0007 §5).

These focus on the M0 host-cap behavior `admit` still enforces (count only non-terminal,
ignore terminal, fail closed on a bad cap, serialize on the resource lock). The
budget/quota and reserve/idempotency behavior lives in
``test_admission_budget_quota.py``; here budget + quota are seeded generous so the host
cap is the binding constraint. Real Postgres; injected contexts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.services.allocation_admission import (
    AllocationRequest,
    admit,
)
from tests.db_waits import wait_until_backend_waiting

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=1, memory_gb=0, cost_class="local")


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def _admit(conn: psycopg.AsyncConnection, resource: Resource):  # type: ignore[no-untyped-def]
    return admit(
        conn,
        AllocationRequest(ctx=CTX, resource=resource, project="proj", selector=SEL, window=1),
    )


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


async def _seed_resource(conn: psycopg.AsyncConnection, *, cap: object) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: cap, "vcpus": 64, "memory_mb": 65536},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, state: AllocationState
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
        ),
    )


async def _count_allocs(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM allocations")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_admit_under_cap_grants_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=2)
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res)
            assert outcome.granted is True
            assert outcome.allocation is not None
            assert outcome.allocation.state is AllocationState.GRANTED
            assert await _count_allocs(conn) == 1
            assert await _count_audit(conn) == 1

    asyncio.run(_run())


def test_admit_at_cap_denies_with_no_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            await _seed_allocation(conn, res.id, AllocationState.GRANTED)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert outcome.reason == "at_capacity"
            assert outcome.in_use == 1 and outcome.cap == 1
            assert await _count_allocs(conn) == 1  # no new row
            assert await _count_audit(conn) == 0  # no audit on denial

    asyncio.run(_run())


def test_admit_ignores_terminal_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            await _seed_allocation(conn, res.id, AllocationState.RELEASED)
            await _seed_allocation(conn, res.id, AllocationState.FAILED)
            outcome = await _admit(conn, res)
            assert outcome.granted is True  # terminal rows do not occupy capacity

    asyncio.run(_run())


def test_admit_counts_only_non_terminal(migrated_url: str) -> None:
    # Four non-terminal + two terminal allocations exist; with cap=4 the host is exactly
    # full, so admit denies and the denial's in_use counts only the four non-terminal.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=4)
            await _seed_budget_quota(conn)
            for state in (
                AllocationState.REQUESTED,
                AllocationState.GRANTED,
                AllocationState.ACTIVE,
                AllocationState.RELEASING,
                AllocationState.RELEASED,
                AllocationState.FAILED,
            ):
                await _seed_allocation(conn, res.id, state)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.in_use == 4  # requested/granted/active/releasing only
            assert outcome.cap == 4

    asyncio.run(_run())


@pytest.mark.parametrize("cap", [None, "two", -1, True])
def test_admit_bad_cap_fails_closed(migrated_url: str, cap: object) -> None:
    # An invalid host cap fails closed as a denial with category configuration_error;
    # admit catches the resolve error and rolls back, so no row survives.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=cap)
            await _seed_budget_quota(conn)
            outcome = await _admit(conn, res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count_allocs(conn) == 0

    asyncio.run(_run())


def test_admit_blocks_behind_a_held_resource_lock(migrated_url: str) -> None:
    # Deterministic proof admit acquires LockScope.RESOURCE: pre-hold it on conn A and
    # assert admit on conn B cannot complete until A releases.
    async def _run() -> None:
        async with (
            _conn(migrated_url) as seed,
            _conn(migrated_url) as a,
            _conn(migrated_url) as b,
        ):
            res = await _seed_resource(seed, cap=1)
            await _seed_budget_quota(seed)
            async with a.transaction(), advisory_xact_lock(a, LockScope.RESOURCE, res.id):
                task = asyncio.ensure_future(_admit(b, res))
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()  # blocked on the resource lock
            # leaving the lock + transaction releases the lock
            outcome = await task
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_two_calls_at_cap_one_grant_one_deny(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget_quota(conn)
            first = await _admit(conn, res)
            second = await _admit(conn, res)
            assert first.granted is True
            assert second.granted is False
            assert await _count_allocs(conn) == 1

    asyncio.run(_run())
