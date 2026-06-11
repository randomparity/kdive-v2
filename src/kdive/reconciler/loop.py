"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs the repairs — allocation
expiry, orphaned System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain,
idempotency-key GC, and (when an image store is wired) the three image-catalog sweeps:
leaked image objects, dangling image rows, and expired private images — each on a fresh
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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import IMAGE_PUBLISH_GRACE
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import JobKind
from kdive.domain.state import AllocationState, DebugSessionState, JobState, RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import PayloadValidationError, SystemPayload, run_id_from_payload
from kdive.providers.reaping import InfraReaper
from kdive.providers.transport_reset import NullResetter, TransportResetter
from kdive.reconciler.console_hosting import CollectorRegistry
from kdive.reconciler.images import (
    ImageSweepStore,
)
from kdive.reconciler.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.images import (
    repair_expired_private_images as _repair_expired_private_images,
)
from kdive.reconciler.images import (
    repair_leaked_images as _repair_leaked_images,
)
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
from kdive.reconciler.provider_reaping import repair_leaked_domains as _repair_leaked_domains
from kdive.reconciler.provider_reaping import (
    repair_leaked_probe_guests as _repair_leaked_probe_guests,
)
from kdive.reconciler.uploads import (
    UploadStore,
)
from kdive.reconciler.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation import promotion as allocation_promotion

if TYPE_CHECKING:
    from kdive.health import Heartbeat

_log = logging.getLogger(__name__)

# The default transport resetter (ADR-0086): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_RESETTER: TransportResetter = NullResetter()

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)
# A queued ``requested`` allocation never placeable past this window is reaped to
# ``failed(queue_timeout)`` (ADR-0069). Sized like the lease cap (24h) so a request that
# could never place does not pin pending capacity indefinitely.
DEFAULT_QUEUE_MAX_WAIT = timedelta(hours=24)
# Idempotency-key rows older than this are GC'd by the reconciler (ADR-0040 §3): the
# append-only request/renew retry-dedup store has no other reaper.
DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)
# Fallback image publish-deadline grace when the config setting is unset (its declared
# default is the same 3600s). A pending image row (or an orphan object with no row) is
# protected from the leaked/dangling image sweeps until this window past pending_since/mtime.
DEFAULT_IMAGE_PUBLISH_GRACE = timedelta(seconds=3600)

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
    leaked_probe_guests: int = 0
    leaked_images: int = 0
    dangling_images: int = 0
    expired_private_images: int = 0
    console_collectors_reaped: int = 0


def _repair_plan(
    *,
    reaper: InfraReaper,
    resetter: TransportResetter,
    upload_store: UploadStore | None,
    image_store: ImageSweepStore | None,
    console_registry: CollectorRegistry | None,
    debug_session_stale_after: timedelta,
    idempotency_retention: timedelta,
    queue_max_wait: timedelta,
    image_publish_grace: timedelta,
) -> tuple[_RepairSpec, ...]:
    repairs = [
        _RepairSpec("expired_allocations", _sweep_expired_allocations),
        _RepairSpec("promoted_allocations", _promote_pending),
        _RepairSpec("queue_timeouts", _reap_queue_timeouts_for(queue_max_wait)),
        _RepairSpec("orphaned_systems", _repair_orphaned_systems),
        _RepairSpec("abandoned_jobs", _repair_abandoned_jobs),
        _RepairSpec(
            "dead_sessions",
            lambda conn: _repair_dead_sessions(conn, debug_session_stale_after, resetter),
        ),
        _RepairSpec("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper)),
        _RepairSpec("leaked_probe_guests", lambda conn: _repair_leaked_probe_guests(conn, reaper)),
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
    if console_registry is not None:
        repairs.append(
            _RepairSpec(
                "console_collectors_reaped",
                lambda conn: _reap_console_collectors(conn, console_registry),
            )
        )
    if image_store is not None:
        repairs.extend(
            (
                _RepairSpec(
                    "leaked_images",
                    lambda conn: _repair_leaked_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "dangling_images",
                    lambda conn: _repair_dangling_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "expired_private_images",
                    lambda conn: _repair_expired_private_images(conn, image_store),
                ),
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


async def _repair_dead_sessions(
    conn: AsyncConnection, stale_after: timedelta, resetter: TransportResetter
) -> int:
    """Detach stale ``live`` debug sessions, then reset each dead transport best-effort.

    A NULL heartbeat is never swept (a just-attached session that has not beaten yet).
    ``stale_after`` is the provisional cadence contract (ADR-0021). After the detach commits,
    each detached session's transport is reset (ADR-0086) so a dead worker's single-client
    gdbstub does not block the next attach with ``transport_conflict`` — best-effort: a reset
    failure is logged and the sweep continues, and a System that already has a fresh ``live``
    gdbstub holder is skipped so a legitimate re-attach is never evicted.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = %s "
            "WHERE state = %s AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s "
            "RETURNING id, run_id, transport, transport_handle",
            (_DETACHED_DEBUG_SESSION_STATE_VALUE, _LIVE_DEBUG_SESSION_STATE_VALUE, stale_after),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
        await _reset_dead_transport(conn, resetter, row)
    return len(rows)


async def _reset_dead_transport(
    conn: AsyncConnection, resetter: TransportResetter, row: dict
) -> None:
    """Reset one detached session's transport, guarded and best-effort (ADR-0086).

    The System lookup and the live-holder guard run inside one short transaction that commits
    **before** the resetter's provider network I/O, so no read snapshot is held open across it.
    """
    async with conn.transaction():
        system_id, domain_name = await _resolve_system(conn, row["run_id"])
        has_live_holder = system_id is not None and await _has_live_gdbstub_holder(conn, system_id)
    if has_live_holder:
        _log.info(
            "reconciler: session %s detached but System %s has a live gdbstub holder; "
            "skipping transport reset",
            row["id"],
            system_id,
        )
        return
    try:
        await resetter.reset(
            transport=row["transport"],
            transport_handle=row["transport_handle"],
            domain_name=domain_name,
        )
    except Exception:  # noqa: BLE001 - a reset failure must not starve the rest of the sweep
        _log.warning(
            "reconciler: resetting dead transport for session %s failed; the next attach "
            "may contend (transport_conflict)",
            row["id"],
            exc_info=True,
        )


async def _resolve_system(conn: AsyncConnection, run_id: UUID) -> tuple[UUID | None, str | None]:
    """Return ``(system_id, domain_name)`` for a Run, or ``(None, None)`` if the Run is gone."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id AS system_id, s.domain_name "
            "FROM runs r JOIN systems s ON s.id = r.system_id WHERE r.id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    return row["system_id"], row["domain_name"]


async def _has_live_gdbstub_holder(conn: AsyncConnection, system_id: UUID) -> bool:
    """True if any debug session for ``system_id`` is currently ``live`` on the gdbstub transport.

    A live holder means the single-client port is legitimately occupied (a new debugger won the
    freed port after our detach), so re-arming it would evict that client (ADR-0086).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM debug_sessions ds JOIN runs r ON r.id = ds.run_id "
            "WHERE r.system_id = %s AND ds.state = %s AND ds.transport = %s LIMIT 1",
            (system_id, _LIVE_DEBUG_SESSION_STATE_VALUE, "gdbstub"),
        )
        return await cur.fetchone() is not None


# A System whose console collector should be reaped: it reached a terminal state, so it will
# never produce more console output. Reusing the orphaned-System terminal set keeps "gone"
# consistent across the reaper classes.
_GONE_SYSTEM_STATE_VALUES = _ORPHANED_SYSTEM_TERMINAL_STATE_VALUES


async def _reap_console_collectors(conn: AsyncConnection, registry: CollectorRegistry) -> int:
    """Finalize+drop console collectors for gone Systems (ADR-0095, AC7).

    For each collector the hosting leader holds, this checks the System's persisted state and,
    if the System is gone (terminal) or no longer exists, **finalizes** the collector — which
    assembles and persists the single console artifact — and **then** drops it. Ordering is the
    reap-never-races-finalize guard: the artifact is persisted before the collector is forgotten,
    so a teardown's console is never discarded before it is stored. ``finalize`` is idempotent,
    so a teardown-path finalize that already ran makes this reap's finalize a no-op.

    A non-leader replica holds an empty registry (it hosts nothing, AC5), so this reaps nothing
    there. Counts the collectors reaped; one structured-log line per reap.
    """
    held = registry.system_ids()
    if not held:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM systems WHERE id = ANY(%s)",
            (list(held),),
        )
        states = {row[0]: row[1] for row in await cur.fetchall()}
    reaped = 0
    for system_id in held:
        state = states.get(system_id)
        if state is not None and state not in _GONE_SYSTEM_STATE_VALUES:
            continue  # still live: the attach-watcher keeps streaming it
        registry.finalize_and_drop(system_id)
        reaped += 1
        _log.info("reconciler: console collector for gone system %s finalized + reaped", system_id)
    return reaped


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    resetter: TransportResetter = _NULL_RESETTER,
    upload_store: UploadStore | None = None,
    image_store: ImageSweepStore | None = None,
    console_registry: CollectorRegistry | None = None,
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
            resetter=resetter,
            upload_store=upload_store,
            image_store=image_store,
            console_registry=console_registry,
            debug_session_stale_after=debug_session_stale_after,
            idempotency_retention=idempotency_retention,
            queue_max_wait=queue_max_wait,
            image_publish_grace=_image_publish_grace(),
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
        leaked_probe_guests=counts["leaked_probe_guests"],
        leaked_images=counts.get("leaked_images", 0),
        dangling_images=counts.get("dangling_images", 0),
        expired_private_images=counts.get("expired_private_images", 0),
        console_collectors_reaped=counts.get("console_collectors_reaped", 0),
    )


def _image_publish_grace() -> timedelta:
    """Resolve the image publish-deadline grace from config (default 3600s)."""
    seconds = config.get(IMAGE_PUBLISH_GRACE)
    if seconds is None:
        return DEFAULT_IMAGE_PUBLISH_GRACE
    return timedelta(seconds=seconds)


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
        resetter: TransportResetter = _NULL_RESETTER,
        upload_store: UploadStore | None = None,
        image_store: ImageSweepStore | None = None,
        console_registry: CollectorRegistry | None = None,
        interval: timedelta = DEFAULT_INTERVAL,
        debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER,
        idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION,
        queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT,
        heartbeat: Heartbeat | None = None,
        heartbeat_tick: timedelta = timedelta(seconds=1),
        telemetry: ReconcilerTelemetry | None = None,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._resetter = resetter
        self._upload_store = upload_store
        self._image_store = image_store
        self._console_registry = console_registry
        self._interval = interval
        self._debug_session_stale_after = debug_session_stale_after
        self._idempotency_retention = idempotency_retention
        self._queue_max_wait = queue_max_wait
        self._heartbeat = heartbeat
        self._heartbeat_tick = heartbeat_tick.total_seconds()
        self._telemetry = telemetry or ReconcilerTelemetry.disabled()

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool,
            self._reaper,
            resetter=self._resetter,
            upload_store=self._upload_store,
            image_store=self._image_store,
            console_registry=self._console_registry,
            debug_session_stale_after=self._debug_session_stale_after,
            idempotency_retention=self._idempotency_retention,
            queue_max_wait=self._queue_max_wait,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once` every ``interval``, surviving a transient pass error.

        The ``/livez`` heartbeat is bumped by a **background ticker** at
        :attr:`_heartbeat_tick` cadence (ADR-0090 §5), *not* per pass — so a single slow
        pass (an over-interval idempotency GC or a large domain sweep) never makes the
        reconciler read not-live; liveness tracks the event loop, not a repair. A wedged
        event loop stops the ticker too and ``/livez`` goes stale. Each pass also opens a
        span and records its duration plus the reconcile-lag (the gap between the
        scheduled and actual start, which grows when a pass overruns its interval).

        ``reconcile_once`` already isolates each repair, so a raise here is a rare
        whole-pass failure (e.g. pool acquisition); it is logged and the loop continues
        — a durable reconciler must not die on one bad pass.
        """
        ticker = self._start_heartbeat_ticker(stop)
        try:
            await self._pass_loop(stop)
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._heartbeat is None:
            return None
        return asyncio.create_task(_tick_until_stop(self._heartbeat, stop, self._heartbeat_tick))

    async def _pass_loop(self, stop: asyncio.Event) -> None:
        interval = self._interval.total_seconds()
        next_due = time.monotonic()
        while not stop.is_set():
            self._telemetry.observe_lag(time.monotonic() - next_due)
            with self._telemetry.pass_span() as span:
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                    span.set_outcome("error")
                    _log.exception("reconcile pass failed; continuing after %ss", interval)
            next_due = time.monotonic() + interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)


async def _tick_until_stop(heartbeat: Heartbeat, stop: asyncio.Event, interval: float) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the pass loop so a long-running pass never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck reconciler still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        heartbeat.tick()
