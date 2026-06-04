"""The metering ledger writers and usage rollup (ADR-0007 §3,6).

The cost ledger hits at **reserve-at-grant, reconcile-at-release** (ADR-0007 §3):

* :func:`reserve` writes a signed ``reserved`` row (`+estimate`) **and** increments the
  project's ``budgets.spent_kcu`` running total in one transaction. Admission (#66) calls
  it under the ``PROJECT`` lock; a renewal (#67) writes an **additional** ``reserved`` row.
* :func:`reconcile` writes a ``reconciled`` row with ``kcu_delta = actual − Σ reserved``
  (summed over **all** of the allocation's reserved rows, so a renewed allocation is not
  over-debited) and applies the same delta to ``spent_kcu`` — again in one transaction.
  ``actual = rate(selector) × active_hours`` where ``active_hours = active_ended_at −
  active_started_at`` read from the allocation row, **never** ``updated_at``. An allocation
  released from ``granted`` without ever going ``active`` has a null ``active_started_at``
  → ``active_hours = 0`` → a full ``−Σ reserved`` credit.

Because the two deltas net to ``actual``, ``spent_kcu`` always equals the ledger Σ — the
O(1) running total is never reconstructed from the append-only ledger on a locked path.

:func:`usage` reports a project's ``spent_kcu`` / ``budget_remaining`` from that running
total (O(1)) plus the ``by_cost_class`` / ``shared_kcu`` breakdown summed off the hot path.
:func:`usage_for_investigation` rolls up only allocations whose Runs are **solely** in that
investigation; a shared allocation is attributed to none and appears only in the project's
``shared_kcu`` — so per-investigation sums never double-count (ADR-0007 §3).

All arithmetic uses :class:`~decimal.Decimal`; every recorded/reported kcu passes through
:func:`~kdive.domain.cost.quantize_kcu`, so the estimate, the reserved row, and the
reconciled credit that price one selector agree to the last place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, LiteralString
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.repositories import LEDGER
from kdive.domain.cost import cost, quantize_kcu, rate, resolve_coeff
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import LedgerEntry, LedgerEventType

if TYPE_CHECKING:
    from kdive.domain.models import Allocation

_SECONDS_PER_HOUR = Decimal(3600)


@dataclass(frozen=True)
class ProjectUsage:
    """A project's spend rollup (ADR-0007 §3,6).

    ``spent_kcu`` and ``budget_remaining`` come from the O(1) running total; a missing
    budget row reads as ``limit_kcu = 0`` (fail-closed). ``by_cost_class`` and
    ``shared_kcu`` are summed from the ledger off the locked hot path. ``shared_kcu``
    isolates the spend of allocations whose Runs span multiple investigations (it is
    already part of ``spent_kcu`` — it is not added on top).
    """

    spent_kcu: Decimal
    budget_remaining: Decimal
    by_cost_class: dict[str, Decimal]
    shared_kcu: Decimal


async def reserve(conn: AsyncConnection, allocation: Allocation, estimate: Decimal) -> None:
    """Write a ``reserved`` ledger row (`+estimate`) and bump ``spent_kcu`` atomically.

    The reservation counts against budget immediately (ADR-0007 §3), so two concurrent
    grants cannot both pass a budget check before either debits. Both writes run in one
    transaction; the caller holds the ``PROJECT`` lock (admission / renew, #66/#67).

    Args:
        conn: An async connection (the transaction is opened here / nested as a savepoint).
        allocation: The granted allocation the reservation belongs to.
        estimate: The signed positive kcu to reserve (already quantized by the caller).
    """
    delta = quantize_kcu(estimate)
    await _write_delta(
        conn,
        allocation,
        event_type=LedgerEventType.RESERVED,
        delta=delta,
    )


async def reconcile(conn: AsyncConnection, allocation: Allocation) -> Decimal:
    """Write the ``reconciled`` credit (``actual − Σ reserved``) and apply it to spend.

    ``actual = rate(selector) × active_hours``; ``active_hours`` is
    ``active_ended_at − active_started_at`` from the allocation row, ``0`` when the
    allocation never went ``active`` (a full credit). Reconciles against the Σ of **all**
    reserved rows so a renewed allocation is not over-debited. Both writes run in one
    transaction under the caller's per-allocation lock (ADR-0040 §4), so release and the
    ``→expired`` sweep can never double-reconcile one allocation.

    An allocation whose project has **no budget row** was never metered (an M0-style
    allocation, or any path that did not reserve against a budget): there is nothing to
    reconcile, so this is a no-op returning ``0`` with no ledger or ``spent_kcu`` write.
    This lets ``allocations.release`` call ``reconcile`` unconditionally without charging
    a phantom credit to a project that never opted into metering.

    Returns:
        The signed ``kcu_delta`` written (negative for a credit), or ``0`` for an
        unmetered allocation (no budget row → no write).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if an active allocation has no
            persisted ``requested_vcpus`` / ``requested_memory_gb`` to price ``actual``.
    """
    if not await _has_budget(conn, allocation.project):
        return Decimal(0)
    reserved_sum = await _reserved_sum(conn, allocation.id)
    actual = await _actual_cost(conn, allocation)
    delta = quantize_kcu(actual - reserved_sum)
    await _write_delta(
        conn,
        allocation,
        event_type=LedgerEventType.RECONCILED,
        delta=delta,
    )
    return delta


async def stamp_active_ended(
    conn: AsyncConnection, allocation: Allocation, ended_at: datetime
) -> Allocation:
    """Stamp ``active_ended_at`` on an allocation that reached ``active``, return it updated.

    The billing interval closes when work stops (``active → releasing`` / ``→ expired``).
    A no-op (returns the allocation unchanged) when the allocation never went ``active``,
    so a release-from-``granted`` keeps a null interval → ``active_hours = 0`` → a full
    credit (ADR-0007 §3). The caller stamps before :func:`reconcile`, which reads the
    stamped value off the returned model.

    The stamp decision reads **committed** DB state, not the caller's ``allocation``
    snapshot (#84): the snapshot can predate a provision-ready ``active_started_at`` stamp
    that the first-System ``provisioning → ready`` edge commits in a separate, ``SYSTEM``-
    locked transaction. ``SELECT ... FOR UPDATE`` takes the allocation row's write lock by
    primary key — it always matches the row, so when that writer's stamp is **in flight**
    this blocks until it commits, then re-reads the committed ``active_started_at`` (a bare
    ``WHERE active_started_at IS NOT NULL`` would not: under READ COMMITTED a row whose
    snapshot value is null is never selected, so a concurrent ``null → value`` commit is
    missed). Being self-locking it closes the window for the release path *and* the
    ``→expired`` sweep, which does not take the allocation row lock before stamping. The
    re-read ``active_started_at`` is carried into the returned model so :func:`reconcile`
    prices the interval the DB actually holds; a null committed start → no stamp, unchanged.

    The caller MUST hold an open transaction (both production callers wrap this in one) so
    the ``FOR UPDATE`` lock survives to the follow-up ``UPDATE``.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT active_started_at FROM allocations WHERE id = %s FOR UPDATE",
            (allocation.id,),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return allocation
        started_at = row[0]
        await cur.execute(
            "UPDATE allocations SET active_ended_at = %s WHERE id = %s",
            (ended_at, allocation.id),
        )
    return allocation.model_copy(
        update={"active_started_at": started_at, "active_ended_at": ended_at}
    )


async def usage(conn: AsyncConnection, project: str) -> ProjectUsage:
    """Return ``project``'s spend rollup: O(1) totals + off-hot-path ledger breakdown.

    ``spent_kcu`` / ``budget_remaining`` read the ``budgets`` running total (a missing
    row reads as ``limit_kcu = 0`` — fail-closed). ``by_cost_class`` and ``shared_kcu``
    are summed from the ledger (the audit trail), not the running total.
    """
    spent, limit = await _budget_totals(conn, project)
    by_cost_class = await _by_cost_class(conn, project)
    shared_kcu = await _shared_kcu(conn, project)
    return ProjectUsage(
        spent_kcu=spent,
        budget_remaining=quantize_kcu(limit - spent),
        by_cost_class=by_cost_class,
        shared_kcu=shared_kcu,
    )


async def usage_for_investigation(conn: AsyncConnection, investigation_id: UUID) -> Decimal:
    """Sum the ledger deltas of allocations whose Runs are **solely** in this investigation.

    An allocation backing Runs in more than one investigation (reprovision-in-place,
    ADR-0038) is attributed to none and appears only in the project's ``shared_kcu`` — so
    per-investigation sums never double-count and never exceed the project total
    (ADR-0007 §3). Returns ``0`` for an investigation with no exclusively-owned allocation.
    """
    total = await _sum_scalar(
        conn,
        "SELECT COALESCE(SUM(l.kcu_delta), 0) "
        "FROM ledger l "
        "WHERE l.allocation_id IN ("
        "    SELECT s.allocation_id "
        "    FROM systems s "
        "    JOIN runs r ON r.system_id = s.id "
        "    GROUP BY s.allocation_id "
        "    HAVING count(DISTINCT r.investigation_id) = 1 "
        "       AND bool_and(r.investigation_id = %s) "
        ")",
        (investigation_id,),
    )
    return quantize_kcu(total)


async def _write_delta(
    conn: AsyncConnection,
    allocation: Allocation,
    *,
    event_type: LedgerEventType,
    delta: Decimal,
) -> None:
    """Append one ledger row and apply ``delta`` to ``spent_kcu`` in one transaction."""
    cost_class = await _cost_class(conn, allocation)
    async with conn.transaction():
        await LEDGER.insert(
            conn,
            LedgerEntry(
                id=uuid4(),
                ts=allocation.created_at,  # overwritten by the DB-authoritative `ts` default
                project=allocation.project,
                allocation_id=allocation.id,
                resource_id=allocation.resource_id,
                cost_class=cost_class,
                event_type=event_type,
                kcu_delta=delta,
            ),
        )
        await _apply_to_spent(conn, allocation.project, delta)


async def _apply_to_spent(conn: AsyncConnection, project: str, delta: Decimal) -> None:
    """Add ``delta`` to ``budgets.spent_kcu`` for ``project`` (caller holds PROJECT lock)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE budgets SET spent_kcu = spent_kcu + %s WHERE project = %s",
            (delta, project),
        )
        if cur.rowcount != 1:
            raise CategorizedError(
                f"project {project!r} has no budget row to charge",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"project": project},
            )


async def _reserved_sum(conn: AsyncConnection, allocation_id: UUID) -> Decimal:
    return await _sum_scalar(
        conn,
        "SELECT COALESCE(SUM(kcu_delta), 0) FROM ledger "
        "WHERE allocation_id = %s AND event_type = %s",
        (allocation_id, LedgerEventType.RESERVED.value),
    )


async def _actual_cost(conn: AsyncConnection, allocation: Allocation) -> Decimal:
    """Return ``rate(selector) × active_hours``; ``0`` if the allocation never went active."""
    hours = _active_hours(allocation)
    if hours == 0:
        return Decimal(0)
    if allocation.requested_vcpus is None or allocation.requested_memory_gb is None:
        raise CategorizedError(
            f"allocation {allocation.id} has no persisted size to reconcile",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"allocation_id": str(allocation.id)},
        )
    coeff = await resolve_coeff(conn, await _cost_class(conn, allocation))
    rate_kcu_per_hr = rate(
        coeff, vcpus=allocation.requested_vcpus, memory_gb=allocation.requested_memory_gb
    )
    return cost(rate_kcu_per_hr, hours)


def _active_hours(allocation: Allocation) -> Decimal:
    """Return the billing interval in hours, ``0`` if the allocation never went active.

    ``active_hours = active_ended_at − active_started_at`` (ADR-0007 §3), read from the
    explicit billing columns, never derived from ``updated_at``.
    """
    if allocation.active_started_at is None or allocation.active_ended_at is None:
        return Decimal(0)
    seconds = Decimal((allocation.active_ended_at - allocation.active_started_at).total_seconds())
    return seconds / _SECONDS_PER_HOUR


async def _has_budget(conn: AsyncConnection, project: str) -> bool:
    """Report whether ``project`` has a budget row (i.e. opted into metering)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM budgets WHERE project = %s", (project,))
        return await cur.fetchone() is not None


async def _budget_totals(conn: AsyncConnection, project: str) -> tuple[Decimal, Decimal]:
    """Return ``(spent_kcu, limit_kcu)``; ``(0, 0)`` for a project with no budget row."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT spent_kcu, limit_kcu FROM budgets WHERE project = %s", (project,))
        row = await cur.fetchone()
    if row is None:
        return Decimal(0), Decimal(0)
    return Decimal(row[0]), Decimal(row[1])


async def _by_cost_class(conn: AsyncConnection, project: str) -> dict[str, Decimal]:
    """Sum the ledger deltas per ``cost_class`` for ``project`` (off the hot path)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT cost_class, SUM(kcu_delta) AS total FROM ledger "
            "WHERE project = %s GROUP BY cost_class",
            (project,),
        )
        rows = await cur.fetchall()
    return {row["cost_class"]: quantize_kcu(Decimal(row["total"])) for row in rows}


async def _shared_kcu(conn: AsyncConnection, project: str) -> Decimal:
    """Sum the ledger deltas of allocations whose Runs span >1 investigation."""
    total = await _sum_scalar(
        conn,
        "SELECT COALESCE(SUM(l.kcu_delta), 0) "
        "FROM ledger l "
        "WHERE l.project = %s AND l.allocation_id IN ("
        "    SELECT s.allocation_id "
        "    FROM systems s "
        "    JOIN runs r ON r.system_id = s.id "
        "    GROUP BY s.allocation_id "
        "    HAVING count(DISTINCT r.investigation_id) > 1 "
        ")",
        (project,),
    )
    return quantize_kcu(total)


async def _sum_scalar(
    conn: AsyncConnection, query: LiteralString, params: tuple[object, ...]
) -> Decimal:
    """Run a ``COALESCE(SUM(...), 0)`` aggregate and return its single scalar as Decimal.

    A ``COALESCE``-wrapped aggregate always returns exactly one row; a missing row would
    be a driver/contract violation, surfaced as a ``RuntimeError`` rather than swallowed.
    """
    async with conn.cursor() as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    if row is None:  # Invariant: a COALESCE(SUM(...)) aggregate always yields one row.
        raise RuntimeError("aggregate query returned no row")
    return Decimal(row[0])


async def _cost_class(conn: AsyncConnection, allocation: Allocation) -> str:
    """Resolve the allocation's cost class from its booked Resource (ADR-0007 §1).

    The class is read from the persisted ``resources.cost_class`` of the Resource the
    allocation books, never from request data — the same fail-closed discipline as the
    coefficient resolve. Carrying it on each ledger row lets a future provider's
    allocations sum into ``by_cost_class`` with zero code change (M1 has only ``local``).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the booked Resource is missing.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT cost_class FROM resources WHERE id = %s", (allocation.resource_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"allocation {allocation.id} books missing resource {allocation.resource_id}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"allocation_id": str(allocation.id)},
        )
    return str(row[0])
