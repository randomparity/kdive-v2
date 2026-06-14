"""Allocation lease and queue repair for the reconciler."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.models import JobKind
from kdive.domain.state import AllocationState, JobState, SystemState
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation import promotion as allocation_promotion
from kdive.services.allocation import release as allocation_release

_log = logging.getLogger(__name__)

SYSTEM_RECONCILER_PRINCIPAL = "system:reconciler"

DEFAULT_QUEUE_MAX_WAIT = timedelta(hours=24)

_TERMINAL_ALLOCATION_STATES = (
    AllocationState.RELEASED,
    AllocationState.EXPIRED,
    AllocationState.FAILED,
)
_EXPIRED_ALLOCATION_STATE = AllocationState.EXPIRED
_EXPIRED_ALLOCATION_STATE_VALUE = _EXPIRED_ALLOCATION_STATE.value
_TERMINAL_ALLOCATION_STATE_VALUES = tuple(state.value for state in _TERMINAL_ALLOCATION_STATES)

_ACTIVE_CAPTURE_JOB_STATES = (JobState.QUEUED, JobState.RUNNING)
_CAPTURE_VMCORE_JOB_KIND_VALUE = JobKind.CAPTURE_VMCORE.value
_ACTIVE_CAPTURE_JOB_STATE_VALUES = tuple(state.value for state in _ACTIVE_CAPTURE_JOB_STATES)

_ACTIVE_ALLOCATION_STATE_VALUE = AllocationState.ACTIVE.value

# A System in one of these states is "live" — it keeps its allocation legitimately occupied.
# This is the complement of admission's `_NON_TERMINAL_SYSTEM` (defined/provisioning/ready/
# reprovisioning/crashed); a `crashed` System whose allocation backs an in-progress
# crash investigation is live, NOT orphaned. Derived from the enum, not literal strings, so
# it cannot drift if SystemState gains a value. Mirrors the sibling
# `reconciler.systems._ORPHANED_SYSTEM_TERMINAL_STATES`.
_LIVE_SYSTEM_STATES = (
    SystemState.DEFINED,
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)
_LIVE_SYSTEM_STATE_VALUES = tuple(state.value for state in _LIVE_SYSTEM_STATES)

# An `active` allocation whose System turned terminal (or is absent) is reclaimed only after
# its row has been settled this long, a belt-and-suspenders guard against the narrow window of
# a concurrent mid-provision write against the same allocation (ADR-0108). Mirrors the 2-min
# `DEFAULT_DEBUG_SESSION_STALE_AFTER` "settled long enough to be safe" precedent.
DEFAULT_ORPHANED_ACTIVE_GRACE = timedelta(minutes=2)


async def promote_pending(conn: AsyncConnection) -> int:
    """Promote the oldest placeable queued request per resource (ADR-0069).

    Delegates to :func:`kdive.services.allocation.promotion.promote_pending`, which replays
    the shared admission gate under ``PROJECT -> RESOURCE -> ALLOCATION``.
    """
    return await allocation_promotion.promote_pending(conn)


def reap_queue_timeouts_for(
    queue_max_wait: timedelta,
) -> Callable[[AsyncConnection], Awaitable[int]]:
    """Bind the max-wait window into the queue_timeout reaper for isolated execution."""

    async def _reap(conn: AsyncConnection) -> int:
        return await allocation_promotion.reap_queue_timeouts(conn, queue_max_wait)

    return _reap


async def reap_queue_timeouts(conn: AsyncConnection, queue_max_wait: timedelta) -> int:
    """Reap queued requests never placeable past ``queue_max_wait``."""
    return await allocation_promotion.reap_queue_timeouts(conn, queue_max_wait)


async def sweep_expired_allocations(conn: AsyncConnection) -> int:
    """Reclaim allocations whose lease window has elapsed (ADR-0036, ADR-0040)."""
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
        except Exception:  # noqa: BLE001 - one allocation must not starve the rest
            _log.warning(
                "reconciler: expiring allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reclaimed


async def _expire_one(conn: AsyncConnection, allocation_id: UUID, project: str) -> bool:
    """Move one allocation to ``expired`` and reconcile under PROJECT -> ALLOCATION."""
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
    """Report whether the allocation's lease is still elapsed under the allocation lock."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT lease_expiry IS NOT NULL AND lease_expiry < now() "
            "FROM allocations WHERE id = %s",
            (allocation_id,),
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


async def reap_orphaned_active_allocations(
    conn: AsyncConnection, grace: timedelta = DEFAULT_ORPHANED_ACTIVE_GRACE
) -> int:
    """Release each `active` allocation whose System is terminal/absent (ADR-0108, #371).

    A failed/interrupted lifecycle run leaves an allocation `active` while its single System
    reached a terminal state (`torn_down`/`failed`) — the teardown job never releases the
    allocation — so it permanently holds its host-cap slot (`active` is in admission's
    `OCCUPYING` set), wedging a `cap=1` host. This is the symmetric complement of
    `repair_orphaned_systems` (terminal allocation + live System -> teardown).

    Candidates are read with no lock: `active`, settled past `grace`, and with no `live`
    System (`NOT EXISTS` a `systems` row in `_LIVE_SYSTEM_STATES`). Each candidate is then
    reclaimed under `PROJECT -> ALLOCATION` (re-checked under the lock), in its own
    transaction, isolated so one failure never starves the rest.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT a.id, a.project FROM allocations a "
            "WHERE a.state = %s AND a.updated_at < now() - %s "
            "  AND NOT EXISTS (SELECT 1 FROM systems s "
            "                  WHERE s.allocation_id = a.id AND s.state = ANY(%s))",
            (_ACTIVE_ALLOCATION_STATE_VALUE, grace, list(_LIVE_SYSTEM_STATE_VALUES)),
        )
        candidates = await cur.fetchall()
    reclaimed = 0
    for candidate in candidates:
        try:
            if await _reclaim_orphaned_active(conn, candidate["id"], candidate["project"]):
                reclaimed += 1
        except Exception:  # noqa: BLE001 - one allocation must not starve the rest
            _log.warning(
                "reconciler: reclaiming orphaned active allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reclaimed


async def _reclaim_orphaned_active(
    conn: AsyncConnection, allocation_id: UUID, project: str
) -> bool:
    """Re-check the orphaned-active predicate under the allocation lock, then release.

    Returns True only when the allocation was released this pass. The no-live-System check is
    re-run as a `precondition` **under** the `PROJECT -> ALLOCATION` lock (held by
    `reclaim_under_lock`, which also runs the release transition), so a System (re)created
    between the candidate read and the lock is not reclaimed — closing the read-then-act gap. A
    concurrent release/expiry that already moved the allocation terminal yields a non-`released`
    outcome, which is skipped (idempotent re-run).
    """

    async def _still_orphaned(locked: AsyncConnection) -> bool:
        return not await _has_live_system(locked, allocation_id)

    outcome = await allocation_release.reclaim_under_lock(
        conn,
        _system_audit_writer(allocation_id),
        allocation_id,
        project=project,
        precondition=_still_orphaned,
    )
    if not outcome.released:
        return False
    _log.info(
        "reconciler: orphaned active allocation %s released (System terminal/absent)",
        allocation_id,
    )
    return True


def _system_audit_writer(allocation_id: UUID) -> allocation_release.AuditWriter:
    """A guard-exempt writer: `record_system` under the reconciler principal (no membership)."""

    async def _write(conn: AsyncConnection, event: audit.AuditEvent) -> None:
        await audit.record_system(conn, principal=SYSTEM_RECONCILER_PRINCIPAL, event=event)

    return _write


async def _has_live_system(conn: AsyncConnection, allocation_id: UUID) -> bool:
    """True if the allocation has any System in a live (non-terminal) state."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM systems WHERE allocation_id = %s AND state = ANY(%s) LIMIT 1",
            (allocation_id, list(_LIVE_SYSTEM_STATE_VALUES)),
        )
        return await cur.fetchone() is not None


async def has_active_capture_job(conn: AsyncConnection, system_id: UUID) -> bool:
    """True if ``system_id`` has a non-terminal ``capture_vmcore`` job."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM jobs "
            "WHERE kind = %s AND state = ANY(%s) AND payload->>'system_id' = %s LIMIT 1",
            (
                _CAPTURE_VMCORE_JOB_KIND_VALUE,
                list(_ACTIVE_CAPTURE_JOB_STATE_VALUES),
                str(system_id),
            ),
        )
        return await cur.fetchone() is not None
