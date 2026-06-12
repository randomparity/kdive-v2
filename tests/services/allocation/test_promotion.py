"""Service-level behavior tests for queued allocation promotion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.models import Allocation, Budget, Quota, Resource, ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.security.audit import args_digest
from kdive.services.allocation.admission import AllocationRequest, admit
from kdive.services.allocation.promotion import promote_pending, reap_queue_timeouts

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _resource(conn: psycopg.AsyncConnection) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: 1,
                "vcpus": 64,
                "memory_mb": 65536,
            },
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _quota(conn: psycopg.AsyncConnection, *, limit: str = "1000000") -> None:
    await BUDGETS.upsert(
        conn,
        Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
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


async def _granted(conn: psycopg.AsyncConnection, resource_id: UUID) -> Allocation:
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
        ),
    )


async def _queued(
    conn: psycopg.AsyncConnection,
    resource: Resource,
    *,
    created_offset: timedelta = timedelta(0),
) -> UUID:
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=RequestContext(principal="bob", agent_session="bob-sess", projects=("proj",)),
            resource=resource,
            project="proj",
            selector=Selector(vcpus=1, memory_gb=0, cost_class="local"),
            window=1,
            on_capacity="queue",
            disk_gb=10,
            requested_kind=None,
            requested_resource_id=resource.id,
        ),
    )
    assert outcome.allocation is not None
    alloc_id = outcome.allocation.id
    if created_offset != timedelta(0):
        await conn.execute(
            "UPDATE allocations SET created_at = now() + %s WHERE id = %s",
            (created_offset, alloc_id),
        )
    return alloc_id


async def _state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    cur = await conn.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def test_promote_pending_grants_after_capacity_frees(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, tuple[str, str] | None]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)

            promoted = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT principal, agent_session FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->granted'",
                (queued,),
            )
            audit_row = await cur.fetchone()
        return promoted, state, audit_row

    assert asyncio.run(_run()) == (1, "granted", ("bob", "bob-sess"))


def test_promote_pending_budget_denial_fails_without_retry(migrated_url: str) -> None:
    async def _run() -> tuple[int, int, str, tuple[str, str] | None, str]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            holder = await _granted(conn, resource.id)
            queued = await _queued(conn, resource)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASING)
            await ALLOCATIONS.update_state(conn, holder.id, AllocationState.RELEASED)
            await conn.execute("UPDATE budgets SET limit_kcu = 0 WHERE project = 'proj'")

            first = await promote_pending(conn)
            second = await promote_pending(conn)
            state = await _state(conn, queued)
            cur = await conn.execute(
                "SELECT principal, args_digest FROM audit_log "
                "WHERE object_id = %s AND transition = 'requested->failed'",
                (queued,),
            )
            audit_row = await cur.fetchone()
        expected_digest = args_digest(
            {
                "reason": "budget_exceeded",
                "project": "proj",
                "resource_id": str(resource.id),
            }
        )
        return first, second, state, audit_row, expected_digest

    first, second, state, audit_row, expected_digest = asyncio.run(_run())
    assert (first, second, state) == (0, 0, "failed")
    assert audit_row == ("system:reconciler", expected_digest)


def test_reap_queue_timeouts_fails_only_aged_requested_rows(migrated_url: str) -> None:
    async def _run() -> tuple[int, str, str]:
        async with _conn(migrated_url) as conn:
            resource = await _resource(conn)
            await _quota(conn)
            await _granted(conn, resource.id)
            aged = await _queued(conn, resource, created_offset=timedelta(hours=-48))
            young = await _queued(conn, resource, created_offset=timedelta(minutes=-5))

            reaped = await reap_queue_timeouts(conn, timedelta(hours=24))
            aged_state = await _state(conn, aged)
            young_state = await _state(conn, young)
        return reaped, aged_state, young_state

    assert asyncio.run(_run()) == (1, "failed", "requested")
