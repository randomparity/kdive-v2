"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs the repairs — the M1
``→expired`` allocation sweep, orphaned System, abandoned (zombie) job, dead
DebugSession, leaked libvirt domain, and the M1 idempotency-key GC — each on a fresh
pooled connection, each fencing its writes, each isolated so one failing repair does not
starve the others. The expiry sweep runs first so an allocation it reclaims orphans its
System in the same pass. Time predicates use Postgres ``now()`` (never a Python clock).
The local-libvirt :class:`InfraReaper` implementation lands with the provider (#15); M0
ships :class:`NullReaper` so the Postgres-only repairs run today.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain import accounting
from kdive.domain.models import JobKind
from kdive.domain.state import AllocationState
from kdive.jobs import queue
from kdive.security import audit

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)
# Idempotency-key rows older than this are GC'd by the reconciler (ADR-0040 §3): the
# append-only request/renew retry-dedup store has no other reaper.
DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)

# Allocation states past which an allocation no longer holds a lease to expire.
_TERMINAL_ALLOCATION_STATES = ("released", "expired", "failed")

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

    expired_allocations: int
    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    idempotency_keys_gcd: int
    failures: tuple[str, ...]


async def _sweep_expired_allocations(conn: AsyncConnection) -> int:
    """Reclaim allocations whose lease window has elapsed (ADR-0036 §4, ADR-0040 §4).

    Selects non-terminal allocations with ``lease_expiry < now()`` and, per allocation
    under ``PROJECT → ALLOCATION`` (the global lock order, ADR-0040 §1): re-reads the
    allocation fenced on a non-terminal state, stamps ``active_ended_at``, transitions it
    ``→ expired``, and writes the ``reconciled`` credit — all in one transaction under the
    per-Allocation lock, the **same** lock ``allocations.release`` takes, so the two can
    never double-reconcile one allocation (ADR-0040 §4). The flip ``→ expired`` orphans
    the allocation's System; the existing :func:`_repair_orphaned_systems` pass (run after
    this one) hands it to the M0 teardown, which honors the in-flight-job grace window — so
    the ``→expired`` flip never bypasses the drain (ADR-0036 §4).

    Idempotent: a second pass selects no row already ``expired``, and the per-allocation
    re-read fences against a release that won the race between select and lock. Counts only
    allocations this pass actually moved to ``expired``; one structured-log line per reclaim.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, project FROM allocations "
            "WHERE state <> ALL(%s) AND lease_expiry IS NOT NULL AND lease_expiry < now()",
            (list(_TERMINAL_ALLOCATION_STATES),),
        )
        candidates = await cur.fetchall()
    reclaimed = 0
    for candidate in candidates:
        if await _expire_one(conn, candidate["id"], candidate["project"]):
            reclaimed += 1
    return reclaimed


async def _expire_one(conn: AsyncConnection, allocation_id: UUID, project: str) -> bool:
    """Move one allocation to ``expired`` + reconcile under PROJECT → ALLOCATION.

    Returns ``True`` if this call performed the transition, ``False`` if it found the
    allocation already terminal (a release won the race) — the single-reconciliation
    fence (ADR-0040 §4).
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        alloc = await ALLOCATIONS.get(conn, allocation_id)
        if alloc is None or alloc.state.value in _TERMINAL_ALLOCATION_STATES:
            return False
        alloc = await accounting.stamp_active_ended(conn, alloc, datetime.now(UTC))
        await ALLOCATIONS.update_state(conn, allocation_id, AllocationState.EXPIRED)
        await audit.record_system(
            conn,
            principal=SYSTEM_RECONCILER_PRINCIPAL,
            tool="reconciler.sweep_expired",
            object_kind="allocations",
            object_id=allocation_id,
            transition=f"{alloc.state.value}->expired",
            args={"allocation_id": str(allocation_id)},
            project=project,
        )
        await accounting.reconcile(conn, alloc)
    _log.info("reconciler: allocation %s lease expired -> expired + reconciled", allocation_id)
    return True


async def _gc_idempotency_keys(conn: AsyncConnection, retention: timedelta) -> int:
    """Delete ``idempotency_keys`` rows older than ``retention`` (ADR-0040 §3).

    The synchronous request/renew retry-dedup store is append-only and has no other
    reaper; rows past the retention window can never serve a live retry. Returns the count
    deleted; one structured-log line when any are reaped.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM idempotency_keys WHERE created_at < now() - %s", (retention,)
        )
        deleted = cur.rowcount
    if deleted:
        _log.info("reconciler: GC'd %d idempotency key(s) past retention", deleted)
    return deleted


async def _repair_orphaned_systems(conn: AsyncConnection) -> int:
    """Enqueue an idempotent GC teardown for each System whose Allocation is gone.

    A System is orphaned when it is non-terminal but its Allocation is terminal —
    ``released``, ``failed``, or ``expired`` ("a System never outlives its Allocation").
    ``expired`` is the M1 lease-reclamation terminal (ADR-0036 §4): the ``→expired`` sweep
    runs earlier in the same pass, so an allocation it reclaims orphans its System here.
    The teardown job carries the ``system:reconciler`` attribution and bypasses the
    tool-layer destructive gate by design (ADR-0021); the teardown handler (#15) drives
    the System to ``torn_down`` honoring the M0 in-flight-job grace window. Counts only a
    genuinely new enqueue (a re-pass on an already-queued teardown is 0).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "WHERE s.state NOT IN ('torn_down', 'failed') "
            "  AND a.state IN ('released', 'failed', 'expired')"
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


async def _repair_dead_sessions(conn: AsyncConnection, stale_after: timedelta) -> int:
    """Detach ``live`` debug sessions whose heartbeat is stale (non-NULL and old).

    A NULL heartbeat is never swept — it may be a session that just attached and has
    not beaten yet. ``stale_after`` is a provisional cadence contract (ADR-0021): the
    debug plane (#16) must beat at most every ``stale_after / 3``.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = 'detached' "
            "WHERE state = 'live' AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s RETURNING id",
            (stale_after,),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
    return len(rows)


async def _repair_leaked_domains(conn: AsyncConnection, reaper: InfraReaper) -> int:
    """Destroy libvirt domains whose tagged System is gone and no teardown is in flight.

    Reap a tagged domain iff its ``systems`` row is absent or ``torn_down`` (guard a)
    and no ``teardown`` job for it is in flight (guard b). Guard (a) protects a
    mid-provision domain (row-first ordering gives it a ``provisioning`` row). The guards
    are read under the per-System advisory lock; ``destroy`` then runs **unlocked** (a
    slow provider call never holds a Postgres lock), so the idempotent-``destroy``
    contract — not the lock — is what makes a concurrent teardown safe. A ``destroy``
    that raises is logged and the pass continues to the next domain.
    """
    domains = await reaper.list_owned()
    reaped = 0
    for domain in domains:
        if domain.system_id is None:
            continue
        async with (
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, domain.system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT 1 FROM systems WHERE id = %s AND state <> 'torn_down'",
                (domain.system_id,),
            )
            has_live_row = await cur.fetchone() is not None
            await cur.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued', 'running') "
                "  AND kind = 'teardown' AND payload->>'system_id' = %s",
                (str(domain.system_id),),
            )
            teardown_in_flight = await cur.fetchone() is not None
        if has_live_row or teardown_in_flight:
            continue
        try:
            await reaper.destroy(domain.name)
        except Exception:  # noqa: BLE001 - one domain's failure must not strand the others
            _log.warning(
                "reconciler: destroy of leaked domain %s failed; retry next pass",
                domain.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: leaked domain %s (system %s) reaped", domain.name, domain.system_id)
    return reaped


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
    idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION,
) -> ReconcileReport:
    """Run the repairs once, each isolated, each on a fresh pooled connection.

    A repair that raises is logged, its name recorded in ``failures``, and the pass
    continues — one repair never starves the others. Returns the partial counts.

    The ``→expired`` allocation sweep runs **first** so that the allocations it moves to
    ``expired`` are seen as orphaning their System by :func:`_repair_orphaned_systems` in
    the **same** pass (ADR-0036 §4); the idempotency-key GC runs last.

    Counts are **best-effort**: a repair that commits some work and then raises (e.g. a
    transient DB error in a later iteration) reports ``0`` for its category and appears
    in ``failures`` — the committed work stands but is not reflected in the count. The
    per-domain ``destroy`` in :func:`_repair_leaked_domains` is caught individually, so
    the irreversible case (a domain destroyed, then a later failure) keeps its count.
    """
    counts: dict[str, int] = {
        "expired_allocations": 0,
        "orphaned_systems": 0,
        "abandoned_jobs": 0,
        "dead_sessions": 0,
        "leaked_domains": 0,
        "idempotency_keys_gcd": 0,
    }
    failures: list[str] = []

    async def _isolated(name: str, repair: Callable[[AsyncConnection], Awaitable[int]]) -> None:
        try:
            async with pool.connection() as conn:
                counts[name] = await repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", name, exc_info=True)
            failures.append(name)

    await _isolated("expired_allocations", _sweep_expired_allocations)
    await _isolated("orphaned_systems", _repair_orphaned_systems)
    await _isolated("abandoned_jobs", _repair_abandoned_jobs)
    await _isolated(
        "dead_sessions", lambda conn: _repair_dead_sessions(conn, debug_session_stale_after)
    )
    await _isolated("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper))
    await _isolated(
        "idempotency_keys_gcd", lambda conn: _gc_idempotency_keys(conn, idempotency_retention)
    )

    return ReconcileReport(
        expired_allocations=counts["expired_allocations"],
        orphaned_systems=counts["orphaned_systems"],
        abandoned_jobs=counts["abandoned_jobs"],
        dead_sessions=counts["dead_sessions"],
        leaked_domains=counts["leaked_domains"],
        idempotency_keys_gcd=counts["idempotency_keys_gcd"],
        failures=tuple(failures),
    )


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        interval: timedelta = DEFAULT_INTERVAL,
        debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
        idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._interval = interval
        self._debug_session_stale_after = debug_session_stale_after
        self._idempotency_retention = idempotency_retention

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool,
            self._reaper,
            debug_session_stale_after=self._debug_session_stale_after,
            idempotency_retention=self._idempotency_retention,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once` every ``interval``, surviving a transient pass error.

        ``reconcile_once`` already isolates each repair, so a raise here is a rare
        whole-pass failure (e.g. pool acquisition); it is logged and the loop continues
        — a durable reconciler must not die on one bad pass.
        """
        interval = self._interval.total_seconds()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                _log.exception("reconcile pass failed; continuing after %ss", interval)
            await asyncio.sleep(interval)
