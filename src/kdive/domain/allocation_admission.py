"""Always-yes, capacity-checked allocation admission (ADR-0023).

``admit`` books a host's :class:`~kdive.domain.models.Allocation` only if the host's
non-terminal allocation count is under the per-host cap. The count and the insert run
inside one transaction holding a per-**resource** advisory lock
(:data:`~kdive.db.locks.LockScope.RESOURCE`), so concurrent requests for the same host
serialize and the cap cannot be overshot. The cap lives on the resource's
``capabilities`` jsonb under :data:`CONCURRENT_ALLOCATION_CAP_KEY`; a missing/invalid
cap fails closed (``configuration_error``), never "unlimited".

This is core (not a provider plane) — the M0 ``AllocationPlane`` is the always-yes path
implemented here; a provider-supplied lease arrives at M1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState
from kdive.security import audit

if TYPE_CHECKING:
    # Annotation-only (PEP 563): keep this domain module free of a runtime mcp import.
    from kdive.mcp.auth import RequestContext

# The resource-capabilities key carrying the per-host concurrent-Allocation cap. Owned
# here (the consumer); the discovery provider imports it to advertise the cap.
CONCURRENT_ALLOCATION_CAP_KEY = "concurrent_allocation_cap"

# States that occupy a capacity slot (terminal released/failed do not).
_NON_TERMINAL = (
    AllocationState.REQUESTED,
    AllocationState.GRANTED,
    AllocationState.ACTIVE,
    AllocationState.RELEASING,
)


@dataclass(frozen=True)
class AdmissionOutcome:
    """The result of an admission attempt."""

    granted: bool
    allocation: Allocation | None
    reason: str | None
    cap: int
    in_use: int


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


async def admit(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    resource: Resource,
    project: str,
) -> AdmissionOutcome:
    """Admit an allocation against ``resource``'s per-host cap.

    Counts non-terminal allocations and, under cap, inserts a ``granted`` Allocation and
    one audit row — atomically, under a per-resource advisory lock. At cap, returns a
    denial with no row written.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource has no valid cap.
    """
    cap = _resolve_cap(resource)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RESOURCE, resource.id):
        in_use = await _count_non_terminal(conn, resource.id)
        if in_use >= cap:
            return AdmissionOutcome(
                granted=False, allocation=None, reason="at_capacity", cap=cap, in_use=in_use
            )
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                resource_id=resource.id,
                state=AllocationState.GRANTED,
                capability_scope={},
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="allocations.request",
            object_kind="allocations",
            object_id=allocation.id,
            transition="->granted",
            args={"resource_id": str(resource.id), "project": project},
            project=project,
        )
        return AdmissionOutcome(
            granted=True, allocation=allocation, reason=None, cap=cap, in_use=in_use
        )
