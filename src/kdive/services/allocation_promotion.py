"""The work-conserving FIFO promotion sweep + queue_timeout reaper (ADR-0069, #165).

The reconciler half of the pending-queue scheduler. :func:`promote_pending` re-runs
selection for each queued ``requested`` allocation from its persisted inputs (PCIe-aware,
cordon-skipping — the host is chosen *at promotion*, never frozen at enqueue) and promotes
the **oldest *placeable*** request per resource to ``granted``: under
``PROJECT → RESOURCE → ALLOCATION`` it replays the **shared** admission gate
(:func:`kdive.services.allocation_admission.capacity_gate` — no forked grant path), stamps
``resource_id``, transitions ``requested → granted``, writes the ``reserved`` debit, and
sets the lease window. Each candidate runs in its own committed transaction so a sibling's
grant is observed by the next candidate's capacity replay — the per-host cap and the
per-project grant quota can never be overshot within one pass.

Work-conserving: scanning oldest-first and re-resolving *all* matching hosts per request, a
younger request placeable on a free host is promoted even while the global-oldest waits on a
different busy host — a free host is never idled behind a request on a busy host.

A **budget recheck failure at promotion terminates** the request (``requested → failed``),
it does not re-queue (ADR-0069 Consequences) — the unique non-queueable
``ALLOCATION_DENIED``. Every other denial (host-cap full, quota full, PCIe-busy or
PCIe-config) is a **wait** (stays ``requested``); :func:`reap_queue_timeouts` terminates the
permanently-unplaceable ones to ``failed(queue_timeout)`` past the max-wait window. The
grant audit is attributed to the queued row's **original** ``(principal, agent_session)``
(ADR-0069 §4), though the sweep itself runs under the service identity.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.cost import Selector
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.pcie import MatchOutcome, PCIeClaim
from kdive.domain.state import AllocationState, ensure_transition
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services import accounting, pcie_claim
from kdive.services.allocation_admission import (
    AllocationRequest,
    capacity_gate,
    price_window_and_estimate,
)

_log = logging.getLogger(__name__)

# The service principal the sweep runs under; the grant audit is re-attributed to the
# queued row's original (principal, agent_session), so this names the *actor*, not the row.
SYSTEM_PROMOTION_PRINCIPAL = "system:reconciler"

_REQUESTED_VALUE = AllocationState.REQUESTED.value
_SECONDS_PER_HOUR = 3600


async def promote_pending(conn: AsyncConnection) -> int:
    """Promote the oldest *placeable* queued request per resource (one pass).

    Candidate-selects every ``requested`` allocation oldest-first (the partial index), then
    attempts each in its own committed transaction. Returns the number promoted to
    ``granted`` this pass. A budget recheck failure terminates a request to ``failed``
    (counted as not promoted); a per-candidate error rolls that candidate back and leaves it
    ``requested`` for the next pass without starving its siblings.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM allocations WHERE state = %s ORDER BY created_at, id",
            (_REQUESTED_VALUE,),
        )
        candidate_ids = [row["id"] for row in await cur.fetchall()]
    promoted = 0
    for alloc_id in candidate_ids:
        try:
            if await _promote_one(conn, alloc_id):
                promoted += 1
        except Exception:  # noqa: BLE001 - one candidate's failure must not starve the rest
            _log.warning(
                "reconciler: promoting allocation %s failed; retry next pass",
                alloc_id,
                exc_info=True,
            )
    return promoted


async def _promote_one(conn: AsyncConnection, alloc_id: UUID) -> bool:
    """Attempt to promote one queued allocation under PROJECT → RESOURCE → ALLOCATION.

    Returns ``True`` only when the candidate was transitioned ``requested → granted`` this
    call. A budget recheck failure terminates it to ``failed`` and returns ``False`` (it is
    not re-queued); any other denial leaves it ``requested`` (a wait) and returns ``False``.
    A locked re-read fences a release that won the race (the row is no longer ``requested``).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT project FROM allocations WHERE id = %s", (alloc_id,))
        proj_row = await cur.fetchone()
    if proj_row is None:
        return False
    project: str = proj_row["project"]
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, project):
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None or alloc.state is not AllocationState.REQUESTED:
            return False  # a release/another pass won the race
        return await _place_or_terminate(conn, alloc)


async def _place_or_terminate(conn: AsyncConnection, alloc: Allocation) -> bool:
    """Re-resolve candidate hosts and promote onto the first placeable one, or terminate.

    Holds the PROJECT lock (the caller). Per candidate host, under RESOURCE → ALLOCATION,
    replays the shared :func:`capacity_gate`: on success grants; on a non-queueable budget
    denial terminates and stops (waiting frees no budget); on any other denial tries the
    next host. After all hosts: leave ``requested`` (a wait).
    """
    candidates = await _candidate_hosts(conn, alloc)
    for resource in candidates:
        terminated, granted = await _try_one_host(conn, alloc, resource)
        if granted:
            return True
        if terminated:
            return False
    return False


async def _try_one_host(
    conn: AsyncConnection, alloc: Allocation, resource: Resource
) -> tuple[bool, bool]:
    """Replay the gate on one host under RESOURCE → ALLOCATION; return (terminated, granted).

    ``(False, True)`` granted; ``(True, False)`` budget recheck terminated the request;
    ``(False, False)`` a wait denial (try the next host / stay queued). RESOURCE is acquired
    **before** ALLOCATION (the global order ``PROJECT → RESOURCE → ALLOCATION``, the caller
    already holds PROJECT): RESOURCE is the capacity lock admit also uses, so promotion and a
    concurrent synchronous admit on the same host serialize on it (no double-grant), and
    ALLOCATION fences the released-while-queued race.
    """
    request = _request_from_queued(alloc, resource)
    window_hours, estimate = await price_window_and_estimate(conn, request)
    async with (
        advisory_xact_lock(conn, LockScope.RESOURCE, resource.id),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc.id),
    ):
        gate = await capacity_gate(conn, request, estimate=estimate)
        if gate.denial is None:
            granted = await _grant_queued(
                conn,
                alloc,
                resource,
                window_hours=window_hours,
                estimate=estimate,
                devices=gate.devices,
            )
            return False, granted
        if _is_budget_terminate(gate.denial):
            await _terminate(conn, alloc, resource, reason="budget_exceeded")
            return True, False
    return False, False


def _is_budget_terminate(denial: object) -> bool:
    """A budget recheck denial: the unique non-queueable ``ALLOCATION_DENIED`` with no reason.

    The host-cap denial shares ``ALLOCATION_DENIED`` but is ``queueable`` and carries
    ``reason="at_capacity"``; the budget denial is ``queueable=False`` with no reason
    (ADR-0069). Routing on ``queueable`` (not the shared category) is the load-bearing
    distinction between terminate (budget) and wait (capacity).
    """
    category = getattr(denial, "category", None)
    queueable = getattr(denial, "queueable", False)
    return category is ErrorCategory.ALLOCATION_DENIED and not queueable


async def _grant_queued(
    conn: AsyncConnection,
    alloc: Allocation,
    resource: Resource,
    *,
    window_hours: Decimal,
    estimate: Decimal,
    devices: list[PCIeClaim],
) -> bool:
    """Stamp resource_id + lease + claim, transition requested → granted, reserve, audit.

    Returns ``True`` when this call performed the grant, ``False`` if the fenced UPDATE
    matched no row (the candidate was no longer ``requested`` — a release that holds the
    ALLOCATION lock won the race). On a no-match the reserve and audit are skipped, so a
    released-while-queued row is never charged a phantom reserve or audited a phantom grant —
    the fence is self-contained, not reliant only on the outer PROJECT lock.
    """
    now = datetime.now(UTC)
    lease_expiry = now + timedelta(seconds=int(window_hours * _SECONDS_PER_HOUR))
    ensure_transition(alloc.state, AllocationState.GRANTED)
    # PCIeClaim is a TypedDict (plain dict at runtime), already JSON-serializable.
    pcie_json = Jsonb([dict(d) for d in devices])
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE allocations "
            "SET state = %s, resource_id = %s, lease_expiry = %s, pcie_claim = %s "
            "WHERE id = %s AND state = %s",
            (
                AllocationState.GRANTED.value,
                resource.id,
                lease_expiry,
                pcie_json,
                alloc.id,
                _REQUESTED_VALUE,
            ),
        )
        if cur.rowcount != 1:
            return False  # the row left `requested` since the locked re-read — skip writes
    granted = alloc.model_copy(
        update={
            "resource_id": resource.id,
            "state": AllocationState.GRANTED,
            "lease_expiry": lease_expiry,
            "pcie_claim": devices,
        }
    )
    await accounting.reserve(conn, granted, estimate)
    await audit.record_system(
        conn,
        principal=alloc.principal,
        agent_session=alloc.agent_session,
        event=audit.AuditEvent(
            tool="allocations.request",
            object_kind="allocations",
            object_id=alloc.id,
            transition="requested->granted",
            args={"resource_id": str(resource.id), "project": alloc.project},
            project=alloc.project,
        ),
    )
    _log.info(
        "reconciler: promoted queued allocation %s -> granted on resource %s",
        alloc.id,
        resource.id,
    )
    return True


async def _terminate(
    conn: AsyncConnection, alloc: Allocation, resource: Resource, *, reason: str
) -> None:
    """Transition requested → failed (a budget recheck terminate); audit, no ledger write."""
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.FAILED)
    await audit.record_system(
        conn,
        principal=SYSTEM_PROMOTION_PRINCIPAL,
        agent_session=alloc.agent_session,
        event=audit.AuditEvent(
            tool="allocations.request",
            object_kind="allocations",
            object_id=alloc.id,
            transition="requested->failed",
            args={"reason": reason, "project": alloc.project, "resource_id": str(resource.id)},
            project=alloc.project,
        ),
    )
    _log.info("reconciler: queued allocation %s -> failed (%s) at promotion", alloc.id, reason)


async def _candidate_hosts(conn: AsyncConnection, alloc: Allocation) -> list[Resource]:
    """Re-resolve the schedulable placement candidates from the queued row's persisted target.

    By-id (``requested_resource_id``) yields the single named host if it is schedulable
    (``available AND NOT cordoned``); by-kind (``requested_kind``) yields every schedulable
    host of the kind, oldest-first, so selection routes around a busy/cordoned host. A
    non-schedulable target yields no candidate, so the request stays ``requested``.
    """
    if alloc.requested_resource_id is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM resources WHERE id = %s AND status = 'available' AND NOT cordoned",
                (alloc.requested_resource_id,),
            )
            row = await cur.fetchone()
        return [Resource.model_validate(row)] if row else []
    if alloc.requested_kind is None:
        return []
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resources WHERE kind = %s AND status = 'available' AND NOT cordoned "
            "ORDER BY created_at, id",
            (alloc.requested_kind,),
        )
        rows = await cur.fetchall()
    candidates = [Resource.model_validate(row) for row in rows]
    if not alloc.requested_pcie_specs:
        return candidates
    return await _pcie_filtered(conn, alloc, candidates)


async def _pcie_filtered(
    conn: AsyncConnection, alloc: Allocation, candidates: list[Resource]
) -> list[Resource]:
    """Best-effort pre-lock filter: keep hosts with a free matching device for every spec.

    Mirrors the synchronous selector's pre-lock PCIe filter; the in-lock
    :func:`capacity_gate` re-resolves authoritatively, so a host that frees a device between
    this filter and the lock is still claimed correctly.
    """
    specs = list(alloc.requested_pcie_specs)
    kept: list[Resource] = []
    for candidate in candidates:
        descriptors = pcie_claim.descriptors_for(candidate)
        claims = await pcie_claim.active_claims(conn, candidate.id)
        try:
            resolution = pcie_claim.resolve_union(specs, descriptors, claims=claims)
        except CategorizedError:
            continue  # malformed spec on this host's view — never a candidate
        if resolution.outcome is MatchOutcome.MATCHED:
            kept.append(candidate)
    return kept


def _request_from_queued(alloc: Allocation, resource: Resource) -> AllocationRequest:
    """Rebuild the admission request from the queued row's persisted inputs.

    Size is the at-enqueue ``requested_*`` snapshot; the PCIe spec union is
    ``requested_pcie_specs``; the lease window was not persisted (#164), so promotion
    resolves the default window via ``resolve_window_hours(None)`` inside pricing — the
    ``window=None`` here selects that default. The ``ctx`` carries the queued row's original
    ``(principal, agent_session)`` so the gate/grant attribute correctly.
    """
    ctx = RequestContext(
        principal=alloc.principal,
        agent_session=alloc.agent_session,
        projects=(alloc.project,),
    )
    selector = Selector(
        vcpus=alloc.requested_vcpus or 0,
        memory_gb=alloc.requested_memory_gb or 0,
        cost_class=resource.cost_class,
    )
    return AllocationRequest(
        ctx=ctx,
        resource=resource,
        project=alloc.project,
        selector=selector,
        window=None,
        disk_gb=alloc.requested_disk_gb,
        shape=alloc.shape,
        pcie_specs=tuple(alloc.requested_pcie_specs),
        requested_kind=alloc.requested_kind,
        requested_resource_id=alloc.requested_resource_id,
    )


async def reap_queue_timeouts(conn: AsyncConnection, max_wait: timedelta) -> int:
    """Reap queued requests never placeable past ``max_wait`` → ``failed(queue_timeout)``.

    Runs **after** the promotion step in a pass, so every aged row already had its placement
    chance this pass. Candidate-selects ``requested`` rows older than ``max_wait`` (the DB
    ``now()`` clock, no Python skew), then per row under ``PROJECT → ALLOCATION`` re-reads
    and re-validates BOTH the ``requested`` state and the age before flipping
    ``requested → failed`` with the distinct ``queue_timeout`` category — never
    ``lease_expired`` (a queued row never held a lease, ADR-0069). A row promoted by the
    earlier step (now ``granted``) is skipped by the locked re-read. Returns the count
    reaped; the credit/active_ended stamps are skipped (a queued row was never reserved).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, project FROM allocations WHERE state = %s AND created_at < now() - %s",
            (_REQUESTED_VALUE, max_wait),
        )
        candidates = await cur.fetchall()
    reaped = 0
    for candidate in candidates:
        try:
            if await _reap_one(conn, candidate["id"], candidate["project"], max_wait):
                reaped += 1
        except Exception:  # noqa: BLE001 - one row's failure must not starve the rest
            _log.warning(
                "reconciler: reaping queued allocation %s failed; retry next pass",
                candidate["id"],
                exc_info=True,
            )
    return reaped


async def _reap_one(
    conn: AsyncConnection, alloc_id: UUID, project: str, max_wait: timedelta
) -> bool:
    """Flip one aged queued row to failed(queue_timeout) under PROJECT → ALLOCATION.

    The locked re-read re-validates the ``requested`` state and the age predicate so a row
    promoted or cancelled since the candidate select is skipped (it is no longer
    ``requested``), and a renewed/younger row is never reaped on a stale read.
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id),
    ):
        if not await _still_aged_requested(conn, alloc_id, max_wait):
            return False
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None:  # Invariant: the age check matched the row.
            return False
        await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.FAILED)
        await audit.record_system(
            conn,
            principal=SYSTEM_PROMOTION_PRINCIPAL,
            agent_session=alloc.agent_session,
            event=audit.AuditEvent(
                tool="reconciler.reap_queue_timeout",
                object_kind="allocations",
                object_id=alloc_id,
                transition="requested->failed",
                args={"reason": ErrorCategory.QUEUE_TIMEOUT.value, "project": project},
                project=project,
            ),
        )
    _log.info("reconciler: queued allocation %s -> failed (queue_timeout)", alloc_id)
    return True


async def _still_aged_requested(conn: AsyncConnection, alloc_id: UUID, max_wait: timedelta) -> bool:
    """Locked re-read: the row is still ``requested`` AND still older than ``max_wait``."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT state = %s AND created_at < now() - %s FROM allocations WHERE id = %s",
            (_REQUESTED_VALUE, max_wait, alloc_id),
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False
