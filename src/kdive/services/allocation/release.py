"""Shared allocation release mechanics for project and break-glass callers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation.error_details import categorized_details

AuditWriter = Callable[[AsyncConnection, audit.AuditEvent], Awaitable[None]]

_RELEASABLE = (AllocationState.GRANTED, AllocationState.ACTIVE)
_TERMINAL = (AllocationState.RELEASED, AllocationState.EXPIRED, AllocationState.FAILED)


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    """Transport-neutral result of an allocation release attempt."""

    released: bool
    category: ErrorCategory | None = None
    current_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def ctx_audit_writer(ctx: RequestContext) -> AuditWriter:
    """The membership-guarded audit writer used by normal project release."""

    async def _write(conn: AsyncConnection, event: audit.AuditEvent) -> None:
        await audit.record(conn, ctx, event)

    return _write


async def release_with_backstops(
    pool: AsyncConnectionPool,
    uid: UUID,
    *,
    project: str,
    audit_writer: AuditWriter,
) -> ReleaseOutcome:
    """Release an allocation and map transition/reconcile failures to service outcomes."""
    async with pool.connection() as conn:
        try:
            return await _release_locked(conn, audit_writer, uid, project=project)
        except IllegalTransition:
            async with pool.connection() as conn2:
                latest = await ALLOCATIONS.get(conn2, uid)
            return ReleaseOutcome(
                released=False,
                category=ErrorCategory.CONFIGURATION_ERROR,
                current_status=latest.state.value if latest else None,
            )
        except CategorizedError as exc:
            return ReleaseOutcome(
                released=False,
                category=exc.category,
                details=categorized_details(exc),
            )


async def _transition_and_audit(
    conn: AsyncConnection,
    audit_writer: AuditWriter,
    alloc_id: UUID,
    frm: AllocationState,
    to: AllocationState,
    *,
    project: str,
) -> None:
    await ALLOCATIONS.update_state(conn, alloc_id, to)
    await audit_writer(
        conn,
        audit.AuditEvent(
            tool="allocations.release",
            object_kind="allocations",
            object_id=alloc_id,
            transition=f"{frm.value}->{to.value}",
            args={"allocation_id": str(alloc_id)},
            project=project,
        ),
    )


LockedPrecondition = Callable[[AsyncConnection], Awaitable[bool]]


async def reclaim_under_lock(
    conn: AsyncConnection,
    audit_writer: AuditWriter,
    uid: UUID,
    *,
    project: str,
    precondition: LockedPrecondition | None = None,
) -> ReleaseOutcome:
    """Release an `active` allocation on a caller-held connection, taking PROJECT -> ALLOCATION.

    The connection-based sibling of :func:`release_with_backstops` for callers (the reconciler's
    orphaned-active reaper, ADR-0109) that already hold a pooled connection and a system audit
    writer and must not nest a second pool acquisition. It runs the identical release body —
    ``active -> releasing -> released`` with the ``active_ended_at`` stamp and the single
    ``reconciled`` credit — under the same advisory locks, so the reaper, a project release, and
    the expiry sweep serialize and never double-reconcile.

    ``precondition`` (optional) is evaluated **under the lock**, after the re-read confirms the
    allocation is still releasable: returning ``False`` aborts with a neutral non-``released``
    outcome (no transition, no credit). The reaper uses it to re-check "no live System" inside
    the lock, closing the read-then-act gap. Unlike :func:`release_with_backstops`, this does
    **not** map ``IllegalTransition`` / ``CategorizedError`` to a neutral outcome: the caller
    isolates each candidate, so the exception propagates to roll back this candidate.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, uid),
    ):
        current = await ALLOCATIONS.get(conn, uid)
        if current is None:
            return ReleaseOutcome(released=False, category=ErrorCategory.CONFIGURATION_ERROR)
        if current.state in _TERMINAL:
            return ReleaseOutcome(
                released=False,
                category=ErrorCategory.STALE_HANDLE,
                current_status=current.state.value,
            )
        if current.state not in _RELEASABLE:
            return ReleaseOutcome(
                released=False,
                category=ErrorCategory.CONFIGURATION_ERROR,
                current_status=current.state.value,
            )
        if precondition is not None and not await precondition(conn):
            return ReleaseOutcome(released=False, current_status=current.state.value)
        await _transition_and_audit(
            conn, audit_writer, uid, current.state, AllocationState.RELEASING, project=project
        )
        current = await accounting.stamp_active_ended(conn, current, datetime.now(UTC))
        await _transition_and_audit(
            conn,
            audit_writer,
            uid,
            AllocationState.RELEASING,
            AllocationState.RELEASED,
            project=project,
        )
        await accounting.reconcile(conn, current)
    return ReleaseOutcome(released=True)


async def _release_locked(
    conn: AsyncConnection, audit_writer: AuditWriter, uid: UUID, *, project: str
) -> ReleaseOutcome:
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, uid),
    ):
        current = await ALLOCATIONS.get(conn, uid)
        if current is None:
            return ReleaseOutcome(released=False, category=ErrorCategory.CONFIGURATION_ERROR)
        if current.state in _TERMINAL:
            return ReleaseOutcome(
                released=False,
                category=ErrorCategory.STALE_HANDLE,
                current_status=current.state.value,
            )
        if current.state is AllocationState.REQUESTED:
            # Cancelling a queued row (ADR-0069): a ``requested`` allocation was never
            # reserved and never held a lease, so it releases DIRECTLY to ``released`` —
            # NOT through the ``releasing`` hop (``requested → releasing`` is illegal) — and
            # writes NO ledger credit and NO ``active_ended_at`` stamp. Writing a credit
            # would mint a spurious negative delta (the ADR-0007 §2 budget-minting hazard).
            await _transition_and_audit(
                conn, audit_writer, uid, current.state, AllocationState.RELEASED, project=project
            )
            return ReleaseOutcome(released=True)
        if current.state not in (*_RELEASABLE, AllocationState.RELEASING):
            return ReleaseOutcome(
                released=False,
                category=ErrorCategory.CONFIGURATION_ERROR,
                current_status=current.state.value,
            )
        if current.state in _RELEASABLE:
            await _transition_and_audit(
                conn, audit_writer, uid, current.state, AllocationState.RELEASING, project=project
            )
            current = await accounting.stamp_active_ended(conn, current, datetime.now(UTC))
        await _transition_and_audit(
            conn,
            audit_writer,
            uid,
            AllocationState.RELEASING,
            AllocationState.RELEASED,
            project=project,
        )
        await accounting.reconcile(conn, current)
    return ReleaseOutcome(released=True)
