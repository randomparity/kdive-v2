"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs four repairs — orphaned
System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain — each on a
fresh pooled connection, each fencing its writes, each isolated so one failing repair
does not starve the others. Time predicates use Postgres ``now()`` (never a Python
clock). The local-libvirt :class:`InfraReaper` implementation lands with the provider
(#15); M0 ships :class:`NullReaper` so the three Postgres-only repairs run today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.models import JobKind
from kdive.jobs import queue

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)

# Reserved principal for system-initiated GC teardowns (ADR-0021): a reconciler
# teardown bypasses the interactive destructive-op gate by design, made auditable
# by this attribution rather than the owning user's.
SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"


@runtime_checkable
class OwnedDomain(Protocol):
    """A libvirt domain the provider owns; ``system_id`` is its metadata tag."""

    name: str
    system_id: UUID | None


@runtime_checkable
class InfraReaper(Protocol):
    """The narrow provider port the reconciler consumes (a subset of DiscoveryPlane)."""

    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...


class NullReaper:
    """The M0 default reaper: owns nothing, destroys nothing.

    Until the libvirt provider (#15) ships a real :class:`InfraReaper`, this lets the
    three Postgres-only repairs run in production; leaked-domain reaping activates when
    #15 injects the real reaper. It is the honest "no provider yet" default, not a stub.
    """

    async def list_owned(self) -> list[OwnedDomain]:
        return []

    async def destroy(self, name: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    failures: tuple[str, ...]


async def _repair_orphaned_systems(conn: AsyncConnection) -> int:
    """Enqueue an idempotent GC teardown for each System whose Allocation is gone.

    A System is orphaned when it is non-terminal but its Allocation is ``released`` or
    ``failed`` ("a System never outlives its Allocation"). The teardown job carries the
    ``system:reconciler`` attribution and bypasses the tool-layer destructive gate by
    design (ADR-0021); the teardown handler (#15) drives the System to ``torn_down``.
    Counts only a genuinely new enqueue (a re-pass on an already-queued teardown is 0).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "WHERE s.state NOT IN ('torn_down', 'failed') "
            "  AND a.state IN ('released', 'failed')"
        )
        candidates = await cur.fetchall()
    enqueued = 0
    for candidate in candidates:
        system_id: UUID = candidate["id"]
        dedup_key = f"{system_id}:teardown"
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
                fresh = await cur.fetchone()
                if fresh is None or fresh["state"] in ("torn_down", "failed"):
                    continue
                await cur.execute("SELECT 1 FROM jobs WHERE dedup_key = %s", (dedup_key,))
                already_queued = await cur.fetchone() is not None
            await queue.enqueue(
                conn,
                JobKind.TEARDOWN,
                {"system_id": str(system_id)},
                {
                    "principal": SYSTEM_RECONCILER_PRINCIPAL,
                    "agent_session": None,
                    "project": candidate["project"],
                },
                dedup_key,
            )
        if not already_queued:
            enqueued += 1
            _log.info("reconciler: orphaned system %s -> teardown job enqueued", system_id)
    return enqueued


async def _repair_abandoned_jobs(conn: AsyncConnection) -> int:
    """Dead-letter zombie jobs the worker can never reclaim, compensating their Run.

    A zombie is ``running`` with a lapsed lease and ``attempt >= max_attempts`` —
    ``dequeue``'s ``attempt < max_attempts`` predicate excludes it, so only the
    reconciler can sweep it. Each zombie is processed in its own transaction that
    dead-letters the job (fenced on ``state = 'running'``) and, when the payload carries
    a ``run_id`` whose Run is non-terminal, fails that Run — atomically, so a crash
    cannot strand the Run un-compensated.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM jobs "
            "WHERE state = 'running' AND lease_expires_at < now() "
            "  AND attempt >= max_attempts"
        )
        zombie_ids: list[UUID] = [row["id"] for row in await cur.fetchall()]
    swept = 0
    for job_id in zombie_ids:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE jobs SET state = 'failed', error_category = 'lease_expired' "
                "WHERE id = %s AND state = 'running' RETURNING payload",
                (job_id,),
            )
            row = await cur.fetchone()
            if row is None:  # fence missed: a worker finalized it first
                continue
            run_id = row["payload"].get("run_id")
            if run_id is not None:
                await cur.execute(
                    "UPDATE runs SET state = 'failed', failure_category = 'lease_expired' "
                    "WHERE id = %s AND state IN ('created', 'running')",
                    (UUID(run_id),),
                )
        swept += 1
        _log.info("reconciler: abandoned job %s -> failed (lease_expired)", job_id)
    return swept
