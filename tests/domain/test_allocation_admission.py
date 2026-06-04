"""Tests for capacity admission (ADR-0023). Real Postgres; injected contexts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.allocation_admission import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    admit,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(conn: psycopg.AsyncConnection, *, cap: object) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={CONCURRENT_ALLOCATION_CAP_KEY: cap},
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
            outcome = await admit(conn, CTX, resource=res, project="proj")
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
            await _seed_allocation(conn, res.id, AllocationState.GRANTED)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.granted is False
            assert outcome.allocation is None
            assert outcome.reason == "at_capacity"
            assert outcome.in_use == 1 and outcome.cap == 1
            assert await _count_allocs(conn) == 1  # no new row
            assert await _count_audit(conn) == 0  # no audit on denial

    asyncio.run(_run())


def test_admit_ignores_terminal_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_allocation(conn, res.id, AllocationState.RELEASED)
            await _seed_allocation(conn, res.id, AllocationState.FAILED)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.granted is True  # terminal rows do not occupy capacity

    asyncio.run(_run())


def test_admit_counts_only_non_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=5)
            for state in (
                AllocationState.REQUESTED,
                AllocationState.GRANTED,
                AllocationState.ACTIVE,
                AllocationState.RELEASING,
                AllocationState.RELEASED,
                AllocationState.FAILED,
            ):
                await _seed_allocation(conn, res.id, state)
            outcome = await admit(conn, CTX, resource=res, project="proj")
            assert outcome.in_use == 4  # requested/granted/active/releasing only

    asyncio.run(_run())


@pytest.mark.parametrize("cap", [None, "two", -1, True])
def test_admit_bad_cap_fails_closed(migrated_url: str, cap: object) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=cap)
            with pytest.raises(CategorizedError) as exc:
                await admit(conn, CTX, resource=res, project="proj")
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
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
            async with a.transaction(), advisory_xact_lock(a, LockScope.RESOURCE, res.id):
                task = asyncio.ensure_future(admit(b, CTX, resource=res, project="proj"))
                await asyncio.sleep(0.3)
                assert not task.done()  # blocked on the resource lock
            # leaving the lock + transaction releases the lock
            outcome = await task
            assert outcome.granted is True

    asyncio.run(_run())


def test_admit_two_calls_at_cap_one_grant_one_deny(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            first = await admit(conn, CTX, resource=res, project="proj")
            second = await admit(conn, CTX, resource=res, project="proj")
            assert first.granted is True
            assert second.granted is False
            assert await _count_allocs(conn) == 1

    asyncio.run(_run())
