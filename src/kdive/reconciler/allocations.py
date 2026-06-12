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
from kdive.domain.state import AllocationState, JobState
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation import promotion as allocation_promotion

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
