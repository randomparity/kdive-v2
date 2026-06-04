"""The `allocations.*` MCP tools — the Allocation admission/lifecycle surface (ADR-0023).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`request` admits against the per-host cap (core `admit`); `release` drives a granted/active
allocation to `released` under a per-allocation advisory lock with an `IllegalTransition`
backstop; `get`/`list` render an allocation through `_envelope_for_allocation`, which maps
the terminal `failed` state to a `failure` envelope (its value collides with the response
envelope's failure-status set). RBAC: `request`/`release` require `operator`; reads require
project membership. Authz denials raise (ADR-0020: no authz `ErrorCategory`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain import accounting
from kdive.domain.allocation_admission import AdmissionOutcome, admit
from kdive.domain.allocation_renew import RenewOutcome, renew
from kdive.domain.cost import Selector
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
_DEFAULT_KIND = "local-libvirt"
_RELEASABLE = (AllocationState.GRANTED, AllocationState.ACTIVE)
_TERMINAL = (AllocationState.RELEASED, AllocationState.EXPIRED, AllocationState.FAILED)


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_allocation(alloc: Allocation) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6)."""
    if alloc.state is AllocationState.FAILED:
        return ToolResponse.failure(
            str(alloc.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": alloc.state.value},
        )
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data={"project": alloc.project},
    )


async def _resolve_resource(
    conn: AsyncConnection, resource_id: UUID | None, kind: str
) -> Resource | None:
    if resource_id is not None:
        return await RESOURCES.get(conn, resource_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resources WHERE kind = %s ORDER BY created_at, id LIMIT 1", (kind,)
        )
        row = await cur.fetchone()
    return Resource.model_validate(row) if row else None


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    vcpus: int,
    memory_gb: int,
    window: object | None = None,
    resource_id: str | None = None,
    kind: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an allocation against the project budget/quota and the selected host's cap.

    Builds the request selector, resolves the target Resource, and runs the M1 admission
    gate (ADR-0007 §5). A grant returns the allocation id; a denial maps to the gate's
    most specific category — ``quota_exceeded`` (over the concurrency cap),
    ``allocation_denied`` (over budget or host cap), or ``configuration_error`` (a
    malformed selector/window or an over-caps size). Requires ``operator`` on ``project``.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        resolved_id = _as_uuid(resource_id) if resource_id is not None else None
        if resource_id is not None and resolved_id is None:
            return _config_error(resource_id)
        selector = Selector(vcpus=vcpus, memory_gb=memory_gb)
        async with pool.connection() as conn:
            resource = await _resolve_resource(conn, resolved_id, kind or _DEFAULT_KIND)
            if resource is None:
                return _config_error(resource_id or (kind or _DEFAULT_KIND))
            outcome = await admit(
                conn,
                ctx,
                resource=resource,
                project=project,
                selector=selector,
                window=window,
                idempotency_key=idempotency_key,
            )
        if outcome.granted and outcome.allocation is not None:
            return ToolResponse.success(
                str(outcome.allocation.id),
                "granted",
                suggested_next_actions=["allocations.get", "allocations.release"],
                data={"resource_id": str(resource.id), "project": project},
            )
        return _denial_response(resource.id, project, outcome)


def _denial_response(resource_id: UUID, project: str, outcome: AdmissionOutcome) -> ToolResponse:
    """Map a denial outcome to its typed failure envelope (category-specific)."""
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data: dict[str, str] = {}
    if outcome.reason is not None:
        data["reason"] = outcome.reason
    if outcome.cap is not None:
        data["cap"] = str(outcome.cap)
    if outcome.in_use is not None:
        data["in_use"] = str(outcome.in_use)
    _log.info("allocation denied for project %s on resource %s: %s", project, resource_id, category)
    return ToolResponse.failure(
        str(resource_id),
        category,
        suggested_next_actions=["allocations.list"],
        data=data,
    )


async def get_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Return an allocation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
        # A row in an ungranted project is indistinguishable from not-found (no leak).
        if alloc is None or alloc.project not in ctx.projects:
            return _config_error(allocation_id)
        return _envelope_for_allocation(alloc)


async def _transition_and_audit(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc_id: UUID,
    frm: AllocationState,
    to: AllocationState,
    *,
    project: str,
) -> None:
    await ALLOCATIONS.update_state(conn, alloc_id, to)
    await audit.record(
        conn,
        ctx,
        tool="allocations.release",
        object_kind="allocations",
        object_id=alloc_id,
        transition=f"{frm.value}->{to.value}",
        args={"allocation_id": str(alloc_id)},
        project=project,
    )


async def release_allocation(
    pool: AsyncConnectionPool, ctx: RequestContext, allocation_id: str
) -> ToolResponse:
    """Drive an allocation to ``released`` (under a per-allocation lock)."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _config_error(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            try:
                return await _release_locked(conn, ctx, uid, project=alloc.project)
            except IllegalTransition:
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # provision path). Caught OUTSIDE the rolled-back transaction; re-read.
                async with pool.connection() as conn2:
                    latest = await ALLOCATIONS.get(conn2, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return ToolResponse.failure(
                    allocation_id, ErrorCategory.CONFIGURATION_ERROR, data=data
                )
            except CategorizedError as exc:
                # Reconciliation cannot price the allocation (e.g. an active allocation
                # with no persisted size). The transaction rolled back, so no terminal
                # transition or ledger row was committed; surface the typed failure.
                return ToolResponse.failure(allocation_id, exc.category)


async def _release_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    """Drive an allocation to ``released`` and reconcile its spend in one transaction.

    Holds ``PROJECT → ALLOCATION`` (the global lock order, ADR-0040 §1): the project lock
    so the ``reconcile`` debit to ``budgets.spent_kcu`` is race-free against admission and
    the ``→expired`` sweep, the allocation lock so release and the sweep cannot both
    reconcile one allocation (ADR-0040 §4). On the ``active → releasing`` edge the billing
    interval is closed (``active_ended_at``) before ``reconcile`` reads it; a terminal
    allocation is a ``stale_handle`` (the sweep or a prior release already reconciled it).
    """
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, uid),
    ):
        current = await ALLOCATIONS.get(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL:
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.STALE_HANDLE,
                suggested_next_actions=["allocations.get"],
                data={"current_status": current.state.value},
            )
        if current.state not in (*_RELEASABLE, AllocationState.RELEASING):
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.CONFIGURATION_ERROR,
                data={"current_status": current.state.value},
            )
        if current.state in _RELEASABLE:
            await _transition_and_audit(
                conn, ctx, uid, current.state, AllocationState.RELEASING, project=project
            )
            current = await accounting.stamp_active_ended(conn, current, datetime.now(UTC))
        await _transition_and_audit(
            conn, ctx, uid, AllocationState.RELEASING, AllocationState.RELEASED, project=project
        )
        await accounting.reconcile(conn, current)
    return ToolResponse.success(str(uid), "released")


async def renew_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    *,
    extend: object,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Extend an allocation's lease window, re-charged and re-checked (ADR-0036 §3).

    Resolves the allocation, requires ``operator`` on its project, and runs the M1 renew
    (under the ``PROJECT`` lock). A success returns the extended allocation id; a denial
    maps to the most specific category — ``configuration_error`` (``extend ≤ 0``, a bad
    id, or the lease already at ``KDIVE_LEASE_MAX``), ``stale_handle`` (a terminal
    allocation), or ``allocation_denied`` (over budget for the added window, window
    unchanged). A replayed ``idempotency_key`` returns the prior result with no second
    extend or charge.
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _config_error(allocation_id)
            require_role(ctx, alloc.project, Role.OPERATOR)
            outcome = await renew(
                conn, ctx, allocation_id=uid, extend=extend, idempotency_key=idempotency_key
            )
        return _renew_response(uid, outcome)


def _renew_response(uid: UUID, outcome: RenewOutcome) -> ToolResponse:
    """Map a :class:`RenewOutcome` to its typed envelope (success or category-specific)."""
    if outcome.renewed and outcome.allocation is not None:
        return ToolResponse.success(
            str(uid),
            outcome.allocation.state.value,
            suggested_next_actions=["allocations.get", "allocations.release"],
            data={"project": outcome.allocation.project},
        )
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data = {"current_status": outcome.current_status} if outcome.current_status else {}
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"],
        data=data,
    )


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> list[ToolResponse]:
    """Return the newest allocations for ``project``, each as an envelope."""
    require_project(ctx, project)
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM allocations WHERE project = %s "
                "ORDER BY created_at DESC, id LIMIT %s",
                (project, capped),
            )
            rows = await cur.fetchall()
        responses: list[ToolResponse] = []
        for row in rows:
            try:
                responses.append(_envelope_for_allocation(Allocation.model_validate(row)))
            except ValueError:
                _log.warning("allocation row violates the response invariant; degraded")
                responses.append(
                    ToolResponse.failure(
                        str(row.get("id", "?")), ErrorCategory.INFRASTRUCTURE_FAILURE
                    )
                )
        return responses


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="allocations.request")
    async def allocations_request(
        project: str,
        vcpus: int,
        memory_gb: int,
        window: float | str | None = None,
        resource_id: str | None = None,
        kind: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        # `window` accepts a number or a decimal string (precise window), or is omitted
        # for the configured lease default; `idempotency_key` makes the synchronous grant
        # retry-safe, scoped to the caller's principal (ADR-0040 §3).
        return await request_allocation(
            pool,
            current_context(),
            project=project,
            vcpus=vcpus,
            memory_gb=memory_gb,
            window=window,
            resource_id=resource_id,
            kind=kind,
            idempotency_key=idempotency_key,
        )

    @app.tool(name="allocations.get")
    async def allocations_get(allocation_id: str) -> ToolResponse:
        return await get_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.release")
    async def allocations_release(allocation_id: str) -> ToolResponse:
        return await release_allocation(pool, current_context(), allocation_id)

    @app.tool(name="allocations.renew")
    async def allocations_renew(
        allocation_id: str,
        extend: float | str,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        # `extend` is the added window in hours (a number or decimal string), validated
        # > 0 and clamped against the remaining KDIVE_LEASE_MAX window; `idempotency_key`
        # makes the synchronous re-charge retry-safe, scoped to the principal (ADR-0040 §3).
        return await renew_allocation(
            pool,
            current_context(),
            allocation_id,
            extend=extend,
            idempotency_key=idempotency_key,
        )

    @app.tool(name="allocations.list")
    async def allocations_list(project: str, limit: int = DEFAULT_LIST_LIMIT) -> list[ToolResponse]:
        return await list_allocations(pool, current_context(), project=project, limit=limit)
