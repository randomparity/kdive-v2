"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs the repairs — allocation
expiry, orphaned System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain,
and idempotency-key GC — each on a fresh
pooled connection, each fencing its writes, each isolated so one failing repair does not
starve the others. The expiry sweep runs first so an allocation it reclaims orphans its
System in the same pass. Time predicates use Postgres ``now()`` (never a Python clock).
Provider reaper contracts live in :mod:`kdive.providers.reaping`; the Postgres-only repair
path can use ``NullReaper`` there when no provider contributes leaked-infra repair.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import JobKind
from kdive.domain.state import AllocationState, DebugSessionState, JobState, RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import PayloadValidationError, SystemPayload, run_id_from_payload
from kdive.providers.reaping import InfraReaper
from kdive.reconciler.provider_reaping import repair_leaked_domains as _repair_leaked_domains
from kdive.reconciler.uploads import (
    UploadStore,
)
from kdive.reconciler.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation import promotion as allocation_promotion

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)
# A queued ``requested`` allocation never placeable past this window is reaped to
# ``failed(queue_timeout)`` (ADR-0069). Sized like the lease cap (24h) so a request that
# could never place does not pin pending capacity indefinitely.
DEFAULT_QUEUE_MAX_WAIT = timedelta(hours=24)
# Idempotency-key rows older than this are GC'd by the reconciler (ADR-0040 §3): the
# append-only request/renew retry-dedup store has no other reaper.
DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)

# Allocation states past which an allocation no longer holds a lease to expire.
_TERMINAL_ALLOCATION_STATES = (
    AllocationState.RELEASED,
    AllocationState.EXPIRED,
    AllocationState.FAILED,
)
_ORPHANED_SYSTEM_TERMINAL_STATES = (SystemState.TORN_DOWN, SystemState.FAILED)
_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_EXPIRED_ALLOCATION_STATE = AllocationState.EXPIRED
_FAILED_JOB_STATE = JobState.FAILED
_RUNNING_JOB_STATE = JobState.RUNNING
_FAILED_RUN_STATE = RunState.FAILED
_DETACHED_DEBUG_SESSION_STATE = DebugSessionState.DETACHED
_LIVE_DEBUG_SESSION_STATE = DebugSessionState.LIVE
_TEARDOWN_JOB_KIND = JobKind.TEARDOWN
_LEASE_EXPIRED_CATEGORY = ErrorCategory.LEASE_EXPIRED

_TERMINAL_ALLOCATION_STATE_VALUES = tuple(state.value for state in _TERMINAL_ALLOCATION_STATES)
_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES = tuple(
    state.value for state in _ORPHANED_SYSTEM_TERMINAL_STATES
)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)
_EXPIRED_ALLOCATION_STATE_VALUE = _EXPIRED_ALLOCATION_STATE.value
_FAILED_JOB_STATE_VALUE = _FAILED_JOB_STATE.value
_RUNNING_JOB_STATE_VALUE = _RUNNING_JOB_STATE.value
_FAILED_RUN_STATE_VALUE = _FAILED_RUN_STATE.value
_DETACHED_DEBUG_SESSION_STATE_VALUE = _DETACHED_DEBUG_SESSION_STATE.value
_LIVE_DEBUG_SESSION_STATE_VALUE = _LIVE_DEBUG_SESSION_STATE.value
_TEARDOWN_JOB_KIND_VALUE = _TEARDOWN_JOB_KIND.value
_LEASE_EXPIRED_CATEGORY_VALUE = _LEASE_EXPIRED_CATEGORY.value

# Reserved principal for system-initiated GC teardowns (ADR-0021): a reconciler
# teardown bypasses the interactive destructive-op gate by design, made auditable
# by this attribution rather than the owning user's.
SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"

type _RepairFn = Callable[[AsyncConnection], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class _RepairSpec:
    name: str
    repair: _RepairFn


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    expired_allocations: int
    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    idempotency_keys_gc_count: int
    failures: tuple[str, ...]
    abandoned_uploads: int = 0
    promoted_allocations: int = 0
    queue_timeouts: int = 0


def _repair_plan(
    *,
    reaper: InfraReaper,
    upload_store: UploadStore | None,
    debug_session_stale_after: timedelta,
    idempotency_retention: timedelta,
    queue_max_wait: timedelta,
) -> tuple[_RepairSpec, ...]:
    repairs = [
        _RepairSpec("expired_allocations", _sweep_expired_allocations),
        _RepairSpec("promoted_allocations", _promote_pending),
        _RepairSpec("queue_timeouts", _reap_queue_timeouts_for(queue_max_wait)),
        _RepairSpec("orphaned_systems", _repair_orphaned_systems),
        _RepairSpec("abandoned_jobs", _repair_abandoned_jobs),
        _RepairSpec(
            "dead_sessions",
            lambda conn: _repair_dead_sessions(conn, debug_session_stale_after),
        ),
        _RepairSpec("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper)),
        _RepairSpec(
            "idempotency_keys_gc_count",
            lambda conn: _gc_idempotency_keys(conn, idempotency_retention),
        ),
    ]
    if upload_store is not None:
        repairs.append(
            _RepairSpec(
                "abandoned_uploads",
                lambda conn: _repair_abandoned_uploads(conn, upload_store),
            )
        )
    return tuple(repairs)


async def _promote_pending(conn: AsyncConnection) -> int:
    """Promote the oldest placeable queued request per resource (ADR-0069).

    Delegates to :func:`kdive.services.allocation.promotion.promote_pending`, which replays
    the shared admission gate under ``PROJECT → RESOURCE → ALLOCATION`` — sharing admission's
    RESOURCE lock and the expiry sweep's ALLOCATION fence, so the sweep never double-grants
    against a synchronous admit nor promotes a released-while-queued row.
    """
    return await allocation_promotion.promote_pending(conn)


def _reap_queue_timeouts_for(
    queue_max_wait: timedelta,
) -> Callable[[AsyncConnection], Awaitable[int]]:
    """Bind the max-wait window into the queue_timeout reaper for ``_isolated``."""

    async def _reap(conn: AsyncConnection) -> int:
        return await allocation_promotion.reap_queue_timeouts(conn, queue_max_wait)

    return _reap


async def _reap_queue_timeouts(conn: AsyncConnection, queue_max_wait: timedelta) -> int:
    """Reap queued requests never placeable past ``queue_max_wait`` (test/direct entry)."""
    return await allocation_promotion.reap_queue_timeouts(conn, queue_max_wait)


async def _sweep_expired_allocations(conn: AsyncConnection) -> int:
    """Reclaim allocations whose lease window has elapsed (ADR-0036 §4, ADR-0040 §4).

    Selects non-terminal allocations with ``lease_expiry < now()`` and, per allocation
    under ``PROJECT → ALLOCATION`` (the global lock order, ADR-0040 §1): re-reads the
    allocation fenced on a non-terminal state, stamps ``active_ended_at``, transitions it
    ``→ expired``, and writes the ``reconciled`` credit — all in one transaction under the
    per-Allocation lock, the **same** lock ``allocations.release`` takes, so the two can
    never double-reconcile one allocation (ADR-0040 §4). The flip ``→ expired`` orphans
    the allocation's System; the existing :func:`_repair_orphaned_systems` pass (run after
    this one) hands it to teardown, which honors the in-flight-job grace window — so
    the ``→expired`` flip never bypasses the drain (ADR-0036 §4).

    Idempotent: a second pass selects no row already ``expired``, and the per-allocation
    re-read fences against work that won the race between select and lock — a release (now
    terminal) or a renew (``lease_expiry`` pushed live again). Counts only allocations this
    pass actually moved to ``expired``; one structured-log line per reclaim.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, project FROM allocations "
            "WHERE state <> ALL(%s) AND lease_expiry IS NOT NULL AND lease_expiry < now()",
            (list(_TERMINAL_ALLOCATION_STATE_VALUES),),
        )
        candidates = await cur.fetchall()
    reclaimed = 0
    for candidate in candidates:
        try:
            if await _expire_one(conn, candidate["id"], candidate["project"]):
                reclaimed += 1
        except Exception:  # noqa: BLE001 - one allocation's failure must not starve the rest
            # E.g. an active allocation with no persisted size cannot be priced
            # (CategorizedError from reconcile); its transaction rolled back, so it stays
            # non-terminal and is retried next pass while siblings still get swept.
            _log.warning(
                "reconciler: expiring allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reclaimed


async def _expire_one(conn: AsyncConnection, allocation_id: UUID, project: str) -> bool:
    """Move one allocation to ``expired`` + reconcile under PROJECT → ALLOCATION.

    Returns ``True`` if this call performed the transition, ``False`` if the locked
    re-read shows the allocation no longer qualifies — it is already terminal (a release
    won the race), or its lease is live again. Both are fences against work that committed
    between the candidate select and this lock:

    * a **release** moves the allocation terminal (the single-reconciliation fence,
      ADR-0040 §4);
    * a **renew** pushes ``lease_expiry`` into the future *without* changing state
      (ADR-0036 §3), so the terminal-state check alone would miss it — the lease window is
      re-validated against ``now()`` so a renewal the project just paid for is never
      clobbered (ADR-0036 §4: the sweep reclaims only an *elapsed* lease).

    ``renew`` takes the ``PROJECT`` lock this call also holds, so once it is acquired the
    re-read observes a committed renewal; the ``now()`` predicate is evaluated in Postgres,
    never against a Python clock.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        alloc = await ALLOCATIONS.get(conn, allocation_id)
        if alloc is None or alloc.state in _TERMINAL_ALLOCATION_STATES:
            return False
        if not await _lease_elapsed(conn, allocation_id):
            return False
        alloc = await accounting.stamp_active_ended(conn, alloc, datetime.now(UTC))
        await ALLOCATIONS.update_state(conn, allocation_id, _EXPIRED_ALLOCATION_STATE)
        await audit.record_system(
            conn,
            principal=SYSTEM_RECONCILER_PRINCIPAL,
            event=audit.AuditEvent(
                tool="reconciler.sweep_expired",
                object_kind="allocations",
                object_id=allocation_id,
                transition=f"{alloc.state.value}->{_EXPIRED_ALLOCATION_STATE_VALUE}",
                args={"allocation_id": str(allocation_id)},
                project=project,
            ),
        )
        await accounting.reconcile(conn, alloc)
    _log.info("reconciler: allocation %s lease expired -> expired + reconciled", allocation_id)
    return True


async def _lease_elapsed(conn: AsyncConnection, allocation_id: UUID) -> bool:
    """Report whether the allocation's lease is still elapsed (``lease_expiry < now()``).

    Re-evaluates the candidate predicate under the per-allocation lock so a renewal that
    extended ``lease_expiry`` after candidate selection is honored. A null ``lease_expiry``
    is not elapsed (an unbounded allocation is never lease-reclaimed).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT lease_expiry IS NOT NULL AND lease_expiry < now() "
            "FROM allocations WHERE id = %s",
            (allocation_id,),
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


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
    ``expired`` is the lease-reclamation terminal (ADR-0036 §4): the ``→expired`` sweep
    runs earlier in the same pass, so an allocation it reclaims orphans its System here.
    The teardown job carries the ``system:reconciler`` attribution and bypasses the
    tool-layer destructive gate by design (ADR-0021); the teardown handler drives
    the System to ``torn_down`` honoring the in-flight-job grace window. Counts only a
    genuinely new enqueue (a re-pass on an already-queued teardown is 0).
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, s.project FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "WHERE s.state <> ALL(%s) "
            "  AND a.state = ANY(%s)",
            (
                list(_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES),
                list(_TERMINAL_ALLOCATION_STATE_VALUES),
            ),
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
                if fresh is None or fresh["state"] in _ORPHANED_SYSTEM_TERMINAL_STATE_VALUES:
                    continue
                await cur.execute("SELECT 1 FROM jobs WHERE dedup_key = %s", (dedup_key,))
                already_queued = await cur.fetchone() is not None
            await queue.enqueue(
                conn,
                _TEARDOWN_JOB_KIND,
                SystemPayload(system_id=str(system_id)),
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
            "WHERE state = %s AND lease_expires_at < now() "
            "  AND attempt >= max_attempts",
            (_RUNNING_JOB_STATE_VALUE,),
        )
        zombie_ids: list[UUID] = [row["id"] for row in await cur.fetchall()]
    swept = 0
    for job_id in zombie_ids:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE jobs SET state = %s, error_category = %s "
                "WHERE id = %s AND state = %s RETURNING kind, payload",
                (
                    _FAILED_JOB_STATE_VALUE,
                    _LEASE_EXPIRED_CATEGORY_VALUE,
                    job_id,
                    _RUNNING_JOB_STATE_VALUE,
                ),
            )
            row = await cur.fetchone()
            if row is None:  # fence missed: a worker finalized it first
                continue
            try:
                run_id = run_id_from_payload(JobKind(row["kind"]), row["payload"])
            except PayloadValidationError as exc:
                _log.warning(
                    "reconciler: abandoned job %s has invalid payload; "
                    "skipping Run compensation: %s",
                    job_id,
                    exc,
                )
                run_id = None
            if run_id is not None:
                await cur.execute(
                    "UPDATE runs SET state = %s, failure_category = %s "
                    "WHERE id = %s AND state = ANY(%s)",
                    (
                        _FAILED_RUN_STATE_VALUE,
                        _LEASE_EXPIRED_CATEGORY_VALUE,
                        run_id,
                        list(_RUN_COMPENSATION_STATE_VALUES),
                    ),
                )
        swept += 1
        _log.info("reconciler: abandoned job %s -> failed (lease_expired)", job_id)
    return swept


async def _repair_dead_sessions(conn: AsyncConnection, stale_after: timedelta) -> int:
    """Detach ``live`` debug sessions whose heartbeat is stale (non-NULL and old).

    A NULL heartbeat is never swept — it may be a session that just attached and has
    not beaten yet. ``stale_after`` is a provisional cadence contract (ADR-0021): the
    debug plane must beat at most every ``stale_after / 3``.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = %s "
            "WHERE state = %s AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s RETURNING id",
            (_DETACHED_DEBUG_SESSION_STATE_VALUE, _LIVE_DEBUG_SESSION_STATE_VALUE, stale_after),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
    return len(rows)


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    upload_store: UploadStore | None = None,
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
    idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION,
    queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT,
) -> ReconcileReport:
    """Run the repairs once, each isolated, each on a fresh pooled connection.

    A repair that raises is logged, its name recorded in ``failures``, and the pass
    continues — one repair never starves the others. Returns the partial counts.

    The ``→expired`` allocation sweep runs **first** so that the allocations it moves to
    ``expired`` are seen as orphaning their System by :func:`_repair_orphaned_systems` in
    the **same** pass (ADR-0036 §4). The **promotion sweep runs right after the expiry
    sweep** so a slot a lease just freed is filled in the same pass; the
    **queue_timeout reaper runs after the promotion sweep** so every aged request already had
    its placement chance this pass (ADR-0069). The idempotency-key GC runs last.

    Counts are **best-effort**: a repair that commits some work and then raises (e.g. a
    transient DB error in a later iteration) reports ``0`` for its category and appears
    in ``failures`` — the committed work stands but is not reflected in the count. The
    per-domain ``destroy`` in :func:`_repair_leaked_domains` is caught individually, so
    the irreversible case (a domain destroyed, then a later failure) keeps its count.
    """
    counts, failures = await _run_repair_plan(
        pool,
        _repair_plan(
            reaper=reaper,
            upload_store=upload_store,
            debug_session_stale_after=debug_session_stale_after,
            idempotency_retention=idempotency_retention,
            queue_max_wait=queue_max_wait,
        ),
    )

    return ReconcileReport(
        expired_allocations=counts["expired_allocations"],
        orphaned_systems=counts["orphaned_systems"],
        abandoned_jobs=counts["abandoned_jobs"],
        dead_sessions=counts["dead_sessions"],
        leaked_domains=counts["leaked_domains"],
        idempotency_keys_gc_count=counts["idempotency_keys_gc_count"],
        failures=tuple(failures),
        abandoned_uploads=counts["abandoned_uploads"],
        promoted_allocations=counts["promoted_allocations"],
        queue_timeouts=counts["queue_timeouts"],
    )


async def _run_repair_plan(
    pool: AsyncConnectionPool, repairs: tuple[_RepairSpec, ...]
) -> tuple[dict[str, int], list[str]]:
    counts = {spec.name: 0 for spec in repairs}
    counts.setdefault("abandoned_uploads", 0)
    failures: list[str] = []
    for spec in repairs:
        try:
            async with pool.connection() as conn:
                counts[spec.name] = await spec.repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", spec.name, exc_info=True)
            failures.append(spec.name)
    return counts, failures


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        upload_store: UploadStore | None = None,
        interval: timedelta = DEFAULT_INTERVAL,
        debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
        idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION,
        queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._upload_store = upload_store
        self._interval = interval
        self._debug_session_stale_after = debug_session_stale_after
        self._idempotency_retention = idempotency_retention
        self._queue_max_wait = queue_max_wait

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool,
            self._reaper,
            upload_store=self._upload_store,
            debug_session_stale_after=self._debug_session_stale_after,
            idempotency_retention=self._idempotency_retention,
            queue_max_wait=self._queue_max_wait,
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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
