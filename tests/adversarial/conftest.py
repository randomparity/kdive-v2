"""Shared fixtures and seeding helpers for the adversarial suite.

Re-exports the disposable-Postgres fixtures (`migrated_url` and its
dependencies) and provides a small connection factory plus row seeders so each
adversarial module races real connections against a freshly migrated schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.allocation_admission import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401

_DT = datetime(2026, 1, 1, tzinfo=UTC)


async def one(cur: psycopg.AsyncCursor[Any]) -> tuple[Any, ...]:
    """Return the next row, asserting it exists (narrows ``Row | None`` for ty)."""
    row = await cur.fetchone()
    assert row is not None
    return row


@asynccontextmanager
async def open_conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """Yield one autocommit async connection, closed on exit."""
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def open_conns(url: str, n: int) -> AsyncIterator[list[psycopg.AsyncConnection]]:
    """Yield ``n`` distinct autocommit async connections, all closed on exit.

    Distinct connections are what makes contention real: advisory locks and row
    locks serialize across connections, never within one.
    """
    conns: list[psycopg.AsyncConnection] = []
    try:
        for _ in range(n):
            conns.append(await psycopg.AsyncConnection.connect(url, autocommit=True))
        yield conns
    finally:
        for conn in conns:
            await conn.close()


async def seed_resource(conn: psycopg.AsyncConnection, *, cap: object) -> Resource:
    """Insert a local-libvirt resource carrying ``cap`` as its concurrent-alloc cap."""
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


async def seed_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, state: AllocationState
) -> Allocation:
    """Insert one allocation in ``state`` against ``resource_id``."""
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


async def seed_run(conn: psycopg.AsyncConnection) -> UUID:
    """Seed the full resource→allocation→system→investigation→run FK chain.

    Returns the ``runs.id`` so callers can exercise ``run_steps``-backed code
    (idempotency) against a row that satisfies every foreign key.
    """
    resource = await seed_resource(conn, cap=10)
    allocation = await seed_allocation(conn, resource.id, AllocationState.GRANTED)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO systems (allocation_id, state, provisioning_profile, "
            "principal, project) VALUES (%s, 'ready', '{}'::jsonb, 'alice', 'proj') "
            "RETURNING id",
            (allocation.id,),
        )
        system_id = (await one(cur))[0]
        await cur.execute(
            "INSERT INTO investigations (title, state, principal, project) "
            "VALUES ('t', 'open', 'alice', 'proj') RETURNING id"
        )
        investigation_id = (await one(cur))[0]
        await cur.execute(
            "INSERT INTO runs (investigation_id, system_id, state, build_profile, "
            "principal, project) VALUES (%s, %s, 'created', '{}'::jsonb, 'alice', 'proj') "
            "RETURNING id",
            (investigation_id, system_id),
        )
        return (await one(cur))[0]


async def count_rows(conn: psycopg.AsyncConnection, table: str) -> int:
    """Return ``count(*)`` for ``table`` (identifier composed via ``psycopg.sql``)."""
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])
