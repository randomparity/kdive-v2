"""Budget/quota + host-cap allocation admission (ADR-0007 §4-6, ADR-0040).

``admit`` is the M1 fail-closed admission gate. It composes M0's per-**host** capacity
cap (ADR-0023) with the M1 per-**project** invariant — a concurrency quota and a spend
budget — and reserves the lease estimate against budget in the same transaction it grants:

1. **Validate first** (no lock, no write): the selector size, the lease window, and the
   selector ≤ the chosen Resource's advertised caps. Any failure is a
   ``configuration_error`` so a negative/oversized request can never reach the ledger
   (ADR-0007 §2 — the budget-minting guard).
2. **Resolve idempotency** under the project lock: a replayed ``(principal,
   idempotency_key)`` returns the originally granted allocation with no second grant,
   reserve, or ``spent_kcu`` change (ADR-0040 §3).
3. **Check then debit** under ``PROJECT`` → ``RESOURCE`` (the global lock order, ADR-0040
   §1): ``max_concurrent_allocations`` (→ ``quota_exceeded``), then ``(limit_kcu −
   spent_kcu) ≥ estimate`` read O(1) from the budget row (→ ``allocation_denied``), then
   the M0 host cap (→ ``allocation_denied``). On success, **in one transaction**: insert
   the ``granted`` Allocation (``lease_expiry``, ``requested_vcpus``/``requested_memory_gb``,
   ``active_started_at`` null), write the ``reserved`` ledger row and bump ``spent_kcu``
   (``accounting.reserve``), record the idempotency key, and write the audit row.

Any failing check returns a denial with **no** durable write (ADR-0023's all-or-nothing
rule). The ``cost_class`` is resolved admission-side from the chosen Resource (unlike
``accounting.estimate``, which prices a hypothetical class with no host).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.cost import (
    Selector,
    cost,
    quantize_kcu,
    rate,
    resolve_coeff,
    validate_against_resource,
    validate_size,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import resolve_window_hours
from kdive.domain.models import Allocation, Resource
from kdive.domain.pcie import MatchOutcome, PCIeClaim, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState
from kdive.security import audit
from kdive.services import accounting, pcie_claim
from kdive.services.allocation_idempotency import (
    record_key,
    resolve_replay,
    within_budget,
)
from kdive.services.pcie_claim import NON_TERMINAL_STATES

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_SECONDS_PER_HOUR = 3600

# The idempotency-store ``kind`` discriminator for a request grant (ADR-0040 §3); the
# renewal path (#67) reuses the store under its own kind.
_REQUEST_KIND = "allocations.request"

# States that occupy a capacity slot (terminal released/expired/failed do not). Shared
# with the PCIe occupancy predicate so the host-cap count and the device-claim count can
# never disagree about which allocations are live (ADR-0068).
_NON_TERMINAL = NON_TERMINAL_STATES


@dataclass(frozen=True)
class AllocationRequest:
    """Inputs for one allocation admission attempt.

    ``selector`` carries the priced size (vcpus / memory_gb) — for a shape-sized request
    the caller resolves the shape to this selector before admission (ADR-0067). ``disk_gb``
    is the resolved disk size persisted as the at-grant snapshot (``requested_disk_gb``);
    ``shape`` is the named preset the size resolved from (``None`` for full-custom), recorded
    as a label, not re-resolved later.

    ``pcie_specs`` is the resolved PCIe device-spec **union** (explicit ``pcie_devices``
    plus a shape's ``pcie_match``) — already composed by the caller. An empty union is a
    non-PCIe request. The specs are resolved to distinct free devices and claimed inside the
    per-Resource lock (ADR-0068), never pre-lock.
    """

    ctx: RequestContext
    resource: Resource
    project: str
    selector: Selector
    window: object | None = None
    idempotency_key: str | None = None
    disk_gb: int | None = None
    shape: str | None = None
    pcie_specs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionOutcome:
    """The result of an admission attempt.

    On a grant, ``allocation`` is the inserted (or replayed) row and ``category`` is
    ``None``. On a denial, ``allocation`` is ``None`` and ``category`` is the most
    specific failure the handler maps to a typed response: ``configuration_error``
    (validation), ``quota_exceeded`` (over the concurrency cap / no quota row), or
    ``allocation_denied`` (over budget / no budget row / over the host cap). ``cap`` /
    ``in_use`` carry the host-cap counters for the denial diagnostic.
    """

    granted: bool
    allocation: Allocation | None
    category: ErrorCategory | None = None
    reason: str | None = None
    cap: int | None = None
    in_use: int | None = None


async def admit(
    conn: AsyncConnection,
    request: AllocationRequest,
) -> AdmissionOutcome:
    """Admit an allocation against the project budget/quota and the host cap.

    Validates inputs, resolves idempotency, runs the check-then-debit under
    ``PROJECT`` → ``RESOURCE``, and on success grants + reserves atomically. See the
    module docstring for the full ordering and the denial categories.

    Args:
        conn: An async connection (the transaction is opened here).
        request: The authenticated principal, target Resource/project, requested size,
            lease window, and optional retry key.

    Returns:
        An :class:`AdmissionOutcome`: a grant, or a typed denial with no durable write.
    """
    try:
        window_hours = resolve_window_hours(request.window)
        validate_size(request.selector)
        validate_against_resource(request.selector, request.resource)
        coeff = await resolve_coeff(conn, request.resource.cost_class)
        # quantize_kcu can fail closed (value-too-large) on an extreme window×size; keep
        # it inside the guard so it returns a typed denial, never an uncaught exception.
        estimate = quantize_kcu(
            cost(
                rate(
                    coeff,
                    vcpus=request.selector.vcpus,
                    memory_gb=request.selector.memory_gb,
                ),
                window_hours,
            )
        )
    except CategorizedError as exc:
        return AdmissionOutcome(granted=False, allocation=None, category=exc.category)
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, request.project):
            return await _admit_under_project_lock(
                conn,
                request,
                window_hours=window_hours,
                estimate=estimate,
            )
    except CategorizedError as exc:
        # The M0 host-cap resolve (decision 5) fails closed on an invalid cap; the
        # transaction rolled back, so no durable write survived.
        return AdmissionOutcome(granted=False, allocation=None, category=exc.category)


async def _admit_under_project_lock(
    conn: AsyncConnection,
    request: AllocationRequest,
    *,
    window_hours: Decimal,
    estimate: Decimal,
) -> AdmissionOutcome:
    """Run idempotency + the check-then-debit holding the PROJECT lock (RESOURCE nested)."""
    if request.idempotency_key is not None:
        replay = await resolve_replay(
            conn,
            principal=request.ctx.principal,
            key=request.idempotency_key,
            kind=_REQUEST_KIND,
            operation_label="request",
        )
        if replay is not None:
            if replay.project != request.project:
                # The key already names a grant in another project. Returning that foreign
                # allocation would be a cross-project replay; the same key cannot mean two
                # requests. Fail closed (the client must use a fresh key per request).
                return AdmissionOutcome(
                    granted=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
                )
            return AdmissionOutcome(granted=True, allocation=replay)
    quota_ok = await _within_alloc_quota(conn, request.project)
    if not quota_ok:
        return AdmissionOutcome(
            granted=False, allocation=None, category=ErrorCategory.QUOTA_EXCEEDED
        )
    budget_ok = await within_budget(conn, request.project, estimate)
    if not budget_ok:
        return AdmissionOutcome(
            granted=False, allocation=None, category=ErrorCategory.ALLOCATION_DENIED
        )
    async with advisory_xact_lock(conn, LockScope.RESOURCE, request.resource.id):
        host = await _host_cap_check(conn, request.resource)
        if host is not None:
            return host
        claim = await _resolve_pcie_claim(conn, request)
        if claim.denial is not None:
            return claim.denial
        return await _grant(
            conn,
            request,
            window_hours=window_hours,
            estimate=estimate,
            claimed_devices=claim.devices,
        )


@dataclass(frozen=True)
class _PCIeClaimResult:
    """The in-lock PCIe resolution: a denial to return, or the devices to claim."""

    denial: AdmissionOutcome | None
    devices: list[PCIeClaim]


async def _resolve_pcie_claim(
    conn: AsyncConnection, request: AllocationRequest
) -> _PCIeClaimResult:
    """Resolve the requested device union to distinct free devices under the held lock.

    A locked read-modify-write (ADR-0068 Consequences): the host's occupancy set is read
    under the per-Resource lock this caller already holds, so two requests cannot both
    resolve the last free device. An empty union short-circuits to no devices. The matcher
    splits the two denial modes — ``CONFIG`` (no host descriptor matches; the card is not
    on this host) maps to ``configuration_error``; ``CAPACITY`` (matches exist but every
    one is claimed) maps to ``allocation_denied``, the queueable case (#164). Malformed
    grammar raises a ``CategorizedError`` that ``admit`` catches and rolls back — no write.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any requested spec is malformed.
    """
    if not request.pcie_specs:
        return _PCIeClaimResult(denial=None, devices=[])
    descriptors = pcie_claim.descriptors_for(request.resource)
    claims = await pcie_claim.active_claims(conn, request.resource.id)
    resolution = pcie_claim.resolve_union(list(request.pcie_specs), descriptors, claims=claims)
    if resolution.outcome is MatchOutcome.MATCHED:
        return _PCIeClaimResult(denial=None, devices=_claims_from(resolution.devices))
    category = (
        ErrorCategory.CONFIGURATION_ERROR
        if resolution.outcome is MatchOutcome.CONFIG
        else ErrorCategory.ALLOCATION_DENIED
    )
    return _PCIeClaimResult(
        denial=AdmissionOutcome(granted=False, allocation=None, category=category),
        devices=[],
    )


def _claims_from(devices: list[PCIeDescriptor]) -> list[PCIeClaim]:
    """Project the matched descriptors to the persisted claim snapshot (no host-local label)."""
    return [
        PCIeClaim(bdf=d["bdf"], vendor_id=d["vendor_id"], device_id=d["device_id"]) for d in devices
    ]


async def _grant(
    conn: AsyncConnection,
    request: AllocationRequest,
    *,
    window_hours: Decimal,
    estimate: Decimal,
    claimed_devices: list[PCIeClaim],
) -> AdmissionOutcome:
    """Insert the granted Allocation, reserve, record the key, and audit (one txn)."""
    now = datetime.now(UTC)  # the DB sets created_at/updated_at; lease_expiry is explicit
    lease_expiry = now + timedelta(seconds=int(window_hours * _SECONDS_PER_HOUR))
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=request.ctx.principal,
            agent_session=request.ctx.agent_session,
            project=request.project,
            resource_id=request.resource.id,
            state=AllocationState.GRANTED,
            lease_expiry=lease_expiry,
            requested_vcpus=request.selector.vcpus,
            requested_memory_gb=request.selector.memory_gb,
            requested_disk_gb=request.disk_gb,
            shape=request.shape,
            capability_scope={},
            pcie_claim=claimed_devices,
        ),
    )
    await accounting.reserve(conn, allocation, estimate)
    if request.idempotency_key is not None:
        await record_key(
            conn,
            principal=request.ctx.principal,
            key=request.idempotency_key,
            project=request.project,
            kind=_REQUEST_KIND,
            allocation_id=allocation.id,
        )
    await audit.record(
        conn,
        request.ctx,
        audit.AuditEvent(
            tool="allocations.request",
            object_kind="allocations",
            object_id=allocation.id,
            transition="->granted",
            args={"resource_id": str(request.resource.id), "project": request.project},
            project=request.project,
        ),
    )
    return AdmissionOutcome(granted=True, allocation=allocation)


async def _host_cap_check(conn: AsyncConnection, resource: Resource) -> AdmissionOutcome | None:
    """The M0 per-host capacity check; return a denial outcome, or ``None`` if under cap.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource has no valid cap.
    """
    cap = _resolve_cap(resource)
    in_use = await _count_non_terminal(conn, resource.id)
    if in_use >= cap:
        return AdmissionOutcome(
            granted=False,
            allocation=None,
            category=ErrorCategory.ALLOCATION_DENIED,
            reason="at_capacity",
            cap=cap,
            in_use=in_use,
        )
    return None


def _resolve_cap(resource: Resource) -> int:
    """Read and validate the per-host cap; fail closed on anything invalid."""
    cap = resource.capabilities.get(CONCURRENT_ALLOCATION_CAP_KEY)
    # bool is an int subclass — reject it explicitly so `True` is not read as cap 1.
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise CategorizedError(
            f"resource {resource.id} has no valid {CONCURRENT_ALLOCATION_CAP_KEY!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"resource_id": str(resource.id), "cap": repr(cap)},
        )
    return cap


async def _count_non_terminal(conn: AsyncConnection, resource_id: object) -> int:
    """Count the host's allocations occupying a capacity slot."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state = ANY(%s)",
            (resource_id, [s.value for s in _NON_TERMINAL]),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


async def _within_alloc_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_concurrent_allocations``.

    Fail-closed: a project with **no quota row** is over quota (ADR-0007 §4 — no silent
    default). Counts the project's non-terminal allocations under the held PROJECT lock.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_concurrent_allocations FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    if row is None:
        return False
    cap = int(row[0])
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE project = %s AND state = ANY(%s)",
            (project, [s.value for s in _NON_TERMINAL]),
        )
        count_row = await cur.fetchone()
    if count_row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(count_row[0]) < cap
