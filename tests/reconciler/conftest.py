"""Fixtures and seeding helpers for the reconciler tests (issue #12).

Reuses the disposable-Postgres fixtures from ``tests/db/conftest.py``. Seeding runs on
an autocommit connection (each insert self-commits); repairs run through a real pool
(non-autocommit) so a regression of the candidate-read transaction-nesting hazard is
caught. ``FakeReaper``/``_FakeDomain`` structurally satisfy the InfraReaper/OwnedDomain
ports (no import cycle — they are duck-typed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.models import (
    Allocation,
    DebugSession,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.reconciler.loop import OwnedDomain
from tests.db.conftest import migrated_url, pg_conn, postgres_url

__all__ = ["migrated_url", "pg_conn", "postgres_url"]

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _FakeDomain:
    """An OwnedDomain stand-in (structural match: ``name`` + ``system_id``)."""

    name: str
    system_id: UUID | None


class FakeReaper:
    """Records ``destroy`` calls and returns scripted owned domains.

    ``fail_on`` names raise from ``destroy`` (after being recorded as attempted), so a
    test can prove one domain's failure does not strand the others.
    """

    def __init__(self, *domains: OwnedDomain, fail_on: frozenset[str] = frozenset()) -> None:
        self._domains: tuple[OwnedDomain, ...] = domains
        self._fail_on = fail_on
        self.destroyed: list[str] = []

    async def list_owned(self) -> list[OwnedDomain]:
        return list(self._domains)

    async def destroy(self, name: str) -> None:
        self.destroyed.append(name)
        if name in self._fail_on:
            raise RuntimeError(f"libvirt destroy of {name} failed")


async def connect(url: str) -> psycopg.AsyncConnection:
    """An autocommit connection for seeding and assertions."""
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def run_repair(
    pool: AsyncConnectionPool, repair: Callable[[psycopg.AsyncConnection], Awaitable[int]]
) -> int:
    """Run one repair on a non-autocommit pool connection (exercises real framing)."""
    async with pool.connection() as conn:
        return await repair(conn)


async def seed_system(
    conn: psycopg.AsyncConnection,
    *,
    system_state: SystemState = SystemState.READY,
    alloc_state: AllocationState = AllocationState.ACTIVE,
) -> UUID:
    """Insert resource -> allocation -> system; return the system id."""
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            pool="p",
            cost_class="c",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            resource_id=resource.id,
            state=alloc_state,
        ),
    )
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            allocation_id=allocation.id,
            state=system_state,
            provisioning_profile={"k": "v"},
        ),
    )
    return system.id


async def seed_run(
    conn: psycopg.AsyncConnection, system_id: UUID, *, run_state: RunState = RunState.RUNNING
) -> UUID:
    """Insert investigation -> run on ``system_id``; return the run id."""
    investigation = await INVESTIGATIONS.insert(
        conn,
        Investigation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            title="t",
            state=InvestigationState.OPEN,
        ),
    )
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            investigation_id=investigation.id,
            system_id=system_id,
            state=run_state,
            build_profile={"cfg": 1},
        ),
    )
    return run.id


async def seed_debug_session(
    conn: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    state: DebugSessionState = DebugSessionState.LIVE,
    heartbeat_ago: timedelta | None = None,
) -> UUID:
    """Insert a debug session; set ``worker_heartbeat_at = now() - heartbeat_ago`` if given.

    ``heartbeat_ago=None`` leaves the heartbeat NULL. The timestamp is set in SQL with
    the DB clock so there is no test-vs-Postgres clock skew.
    """
    session = await DEBUG_SESSIONS.insert(
        conn,
        DebugSession(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project="proj",
            run_id=run_id,
            state=state,
            transport="gdbstub",
            worker_heartbeat_at=None,
        ),
    )
    if heartbeat_ago is not None:
        await conn.execute(
            "UPDATE debug_sessions SET worker_heartbeat_at = now() - %s WHERE id = %s",
            (heartbeat_ago, session.id),
        )
    return session.id


async def seed_running_job(
    conn: psycopg.AsyncConnection,
    dedup_key: str,
    *,
    kind: str = "build",
    payload: dict[str, Any] | None = None,
    lease_seconds: int,
    attempt: int,
    max_attempts: int,
) -> UUID:
    """Insert a ``running`` job with a lease ``lease_seconds`` from now (negative = lapsed).

    Raw SQL because the lease timestamp is relative (``now() + make_interval(...)``) and
    a relative interval cannot be a bound ``timestamptz`` parameter.
    """
    cur = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
        "    lease_expires_at, authorizing, dedup_key) "
        "VALUES (%s, %s, 'running', %s, %s, 'w-dead', now() + make_interval(secs => %s), "
        "    %s, %s) RETURNING id",
        (
            kind,
            Jsonb(payload or {}),
            attempt,
            max_attempts,
            lease_seconds,
            Jsonb({"principal": "reconciler-test", "agent_session": None, "project": "test"}),
            dedup_key,
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]
