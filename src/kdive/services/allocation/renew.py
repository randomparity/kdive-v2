"""Lease renewal: extend a live allocation's window, re-charged and re-checked (ADR-0036 §3).

``renew`` is the lease-extension counterpart of
:func:`kdive.services.allocation.admission.admit`.
It extends a non-terminal allocation's ``lease_expiry`` by a validated ``extend`` window,
clamped so the lease never reaches past ``now + KDIVE_LEASE_MAX`` (ADR-0036 §3), and bills
the project for the *added* span only:

1. **Validate first** (no lock, no write): ``extend > 0`` (``configuration_error`` — the
   same budget-minting guard as the initial window, ADR-0007 §2).
2. **Under the PROJECT lock** (ADR-0040 §1 — renew takes ``PROJECT`` only):
   - resolve idempotency under its own ``allocations.renew`` kind: a replayed
     ``(principal, key)`` returns the prior result with no second extend or charge
     (ADR-0040 §3);
   - re-read the allocation; a terminal one is a ``stale_handle`` (ADR-0036 §3);
   - clamp the extension to the remaining window and re-check budget for the *added*
     window only — over budget is ``allocation_denied`` and leaves the window unchanged
     (fail-closed, ADR-0036 §3);
   - in one transaction: extend ``lease_expiry``, write the incremental ``reserved``
     ledger row + bump ``spent_kcu`` (``accounting.reserve``), record the idempotency
     key, and write the audit row.

A renew that clamps to a zero billable window (the lease is already at the cap) is a
``configuration_error`` — fail-closed, the window stands, rather than silently no-opping a
charge. ``spent_kcu`` correctness rides on the held ``PROJECT`` lock, the same as admission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.cost import (
    cost,
    parse_window_hours,
    quantize_kcu,
    rate,
    resolve_coeff,
    validate_window,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import LeaseExtension, clamp_extension_hours
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation.error_details import categorized_details
from kdive.services.allocation.idempotency import (
    record_key,
    resolve_replay,
    within_budget,
)

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

# The idempotency-store ``kind`` discriminator for a renew (ADR-0040 §3); distinct from
# the request grant's kind so a request key and a renew key never collide in the store.
_RENEW_KIND = "allocations.renew"

_TERMINAL = (AllocationState.RELEASED, AllocationState.EXPIRED, AllocationState.FAILED)


@dataclass(frozen=True)
class RenewOutcome:
    """The result of a renew attempt.

    On success, ``allocation`` is the extended row and ``category`` is ``None``. On a
    denial, ``allocation`` is ``None`` and ``category`` is the most specific failure the
    handler maps: ``configuration_error`` (``extend ≤ 0`` or the lease is already at the
    cap), ``stale_handle`` (a terminal allocation), or ``allocation_denied`` (over budget
    for the added window). ``current_status`` carries the terminal state for the
    ``stale_handle`` diagnostic.
    """

    renewed: bool
    allocation: Allocation | None
    category: ErrorCategory | None = None
    current_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


async def renew(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    allocation_id: UUID,
    extend: object,
    idempotency_key: str | None = None,
) -> RenewOutcome:
    """Extend an allocation's lease window, re-charged and re-checked (ADR-0036 §3).

    Validates ``extend > 0``, then under the ``PROJECT`` lock resolves idempotency,
    re-reads the allocation, clamps the extension, re-checks budget for the added window,
    and on success extends ``lease_expiry`` + writes the incremental reservation in one
    transaction. See the module docstring for the full ordering and denial categories.

    Args:
        conn: An async connection (the transaction is opened here).
        ctx: The authenticated request context (attribution + idempotency principal).
        allocation_id: The allocation to renew.
        extend: The requested extension in hours (a number or decimal string); ``> 0``.
        idempotency_key: An optional client retry key, scoped to ``ctx.principal``.

    Returns:
        A :class:`RenewOutcome`: a renewal, or a typed denial with no durable write.
    """
    try:
        extend_hours = _validate_extend(extend)
    except CategorizedError as exc:
        return RenewOutcome(
            renewed=False,
            allocation=None,
            category=exc.category,
            details=categorized_details(exc),
        )
    alloc = await ALLOCATIONS.get(conn, allocation_id)
    if alloc is None:
        return RenewOutcome(
            renewed=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
        )
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, alloc.project):
            return await _renew_under_project_lock(
                conn,
                ctx,
                allocation_id=allocation_id,
                extend_hours=extend_hours,
                idempotency_key=idempotency_key,
            )
    except CategorizedError as exc:
        # A pricing/charge failure (e.g. no budget row, value-too-large) fails closed; the
        # transaction rolled back, so no extend, reserved row, or spent_kcu write survived.
        return RenewOutcome(
            renewed=False,
            allocation=None,
            category=exc.category,
            details=categorized_details(exc),
        )


def _validate_extend(extend: object) -> Decimal:
    """Parse and validate ``extend`` as a finite ``> 0`` number of hours (fail closed)."""
    parsed = parse_window_hours(extend)
    validate_window(parsed)
    return parsed


async def _renew_under_project_lock(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    allocation_id: UUID,
    extend_hours: Decimal,
    idempotency_key: str | None,
) -> RenewOutcome:
    """Resolve idempotency, clamp, re-check budget, and extend — holding the PROJECT lock."""
    if idempotency_key is not None:
        replay = await resolve_replay(
            conn,
            principal=ctx.principal,
            key=idempotency_key,
            kind=_RENEW_KIND,
            operation_label="renew",
        )
        if replay is not None:
            if replay.id != allocation_id:
                # The key already names a renew of a different allocation; the same key
                # cannot mean two operations. Fail closed (use a fresh key per renew).
                return RenewOutcome(
                    renewed=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
                )
            return RenewOutcome(renewed=True, allocation=replay)
    alloc = await ALLOCATIONS.get(conn, allocation_id)
    if alloc is None:
        return RenewOutcome(
            renewed=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
        )
    if alloc.state in _TERMINAL:
        return RenewOutcome(
            renewed=False,
            allocation=None,
            category=ErrorCategory.STALE_HANDLE,
            current_status=alloc.state.value,
        )
    if alloc.lease_expiry is None:
        # A non-terminal allocation always carries a lease_expiry from the grant; a null
        # here is a consistency violation, not a renewable state.
        return RenewOutcome(
            renewed=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
        )
    now = datetime.now(UTC)
    extension = clamp_extension_hours(alloc.lease_expiry, extend_hours, now)
    if extension.added_hours <= 0:
        # The lease is already at the KDIVE_LEASE_MAX ceiling: nothing to extend or charge.
        return RenewOutcome(
            renewed=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
        )
    estimate = await _extension_estimate(conn, alloc, extension.added_hours)
    if not await within_budget(conn, alloc.project, estimate):
        return RenewOutcome(
            renewed=False, allocation=None, category=ErrorCategory.ALLOCATION_DENIED
        )
    return await _apply_renew(
        conn, ctx, alloc, extension=extension, estimate=estimate, idempotency_key=idempotency_key
    )


async def _apply_renew(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    *,
    extension: LeaseExtension,
    estimate: Decimal,
    idempotency_key: str | None,
) -> RenewOutcome:
    """Extend the lease, reserve the added window, record the key, and audit (one txn)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE allocations SET lease_expiry = %s WHERE id = %s",
            (extension.new_expiry, alloc.id),
        )
    extended = alloc.model_copy(update={"lease_expiry": extension.new_expiry})
    await accounting.reserve(conn, extended, estimate)
    if idempotency_key is not None:
        await record_key(
            conn,
            principal=ctx.principal,
            key=idempotency_key,
            project=alloc.project,
            kind=_RENEW_KIND,
            allocation_id=alloc.id,
        )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="allocations.renew",
            object_kind="allocations",
            object_id=alloc.id,
            transition=f"renew:+{extension.added_hours}h",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return RenewOutcome(renewed=True, allocation=extended)


async def _extension_estimate(
    conn: AsyncConnection, alloc: Allocation, added_hours: Decimal
) -> Decimal:
    """Price the added window: ``rate(selector) × added_hours``, quantized (fail closed).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the allocation has no persisted size
            to price, or the product is too large to quantize.
    """
    if alloc.requested_vcpus is None or alloc.requested_memory_gb is None:
        raise CategorizedError(
            f"allocation {alloc.id} has no persisted size to renew",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"allocation_id": str(alloc.id)},
        )
    coeff = await resolve_coeff(conn, await _cost_class(conn, alloc))
    rate_kcu_per_hr = rate(coeff, vcpus=alloc.requested_vcpus, memory_gb=alloc.requested_memory_gb)
    return quantize_kcu(cost(rate_kcu_per_hr, added_hours))


async def _cost_class(conn: AsyncConnection, alloc: Allocation) -> str:
    """Resolve the allocation's cost class from its booked Resource (fail closed)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT cost_class FROM resources WHERE id = %s", (alloc.resource_id,))
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"allocation {alloc.id} books missing resource {alloc.resource_id}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"allocation_id": str(alloc.id)},
        )
    return str(row[0])
