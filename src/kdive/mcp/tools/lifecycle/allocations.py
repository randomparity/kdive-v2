"""The `allocations.*` MCP tools — the Allocation admission/lifecycle surface (ADR-0023).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`request` admits against the per-host cap (core `admit`); `release` drives a granted/active
allocation to `released` under a per-allocation advisory lock with an `IllegalTransition`
backstop; `get`/`list` render an allocation through `_envelope_for_allocation`, which maps
the terminal `failed` state to a `failure` envelope (its value collides with the response
envelope's failure-status set). RBAC: `request`/`release` require `operator`; reads require
`viewer` on the owning project. Authz denials raise (ADR-0020: no authz `ErrorCategory`).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Resource
from kdive.domain.state import AllocationState
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceById, ResourceByKind
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.services.allocation.admission import AdmissionOutcome
from kdive.services.allocation.release import (
    ReleaseOutcome,
    ctx_audit_writer,
    release_with_backstops,
)
from kdive.services.allocation.renew import RenewOutcome, renew
from kdive.services.allocation.request import (
    AdmissionRequestSpec,
    RequestAdmissionResult,
    denial_details,
    request_admission,
)

_log = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


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


def _spec_from_payload(payload: AllocationRequestPayload) -> AdmissionRequestSpec | ToolResponse:
    resolved_id: UUID | None = None
    kind = ResourceByKind().kind
    if isinstance(payload.resource, ResourceById):
        resolved_id = _as_uuid(payload.resource.resource_id)
        if resolved_id is None:
            return _config_error(payload.resource.resource_id)
    else:
        kind = payload.resource.kind
    return AdmissionRequestSpec(
        resource_id=resolved_id,
        kind=kind,
        shape=payload.shape,
        vcpus=payload.vcpus,
        memory_gb=payload.memory_gb,
        disk_gb=payload.disk_gb,
        window=payload.window,
        pcie_devices=tuple(payload.pcie_devices),
        on_capacity=payload.on_capacity,
    )


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    request: AllocationRequestPayload,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit an allocation against the project budget/quota and the selected host's cap.

    Builds the request selector, resolves the target Resource, and runs the admission
    gate (ADR-0007 §5). A grant returns the allocation id; a denial maps to the gate's
    most specific category — ``quota_exceeded`` (over the concurrency cap),
    ``allocation_denied`` (over budget or host cap), or ``configuration_error`` (a
    malformed selector/window or an over-caps size). Requires ``operator`` on ``project``.

    With ``on_capacity=queue`` a **capacity** denial (host cap or concurrency quota) instead
    enqueues a durable ``requested`` allocation holding only a queue position (ADR-0069), and
    the response reports ``requested``. Budget and configuration denials always hard-deny.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        spec = _spec_from_payload(request)
        if isinstance(spec, ToolResponse):
            return spec
        async with pool.connection() as conn:
            result = await request_admission(
                conn,
                ctx,
                project=project,
                spec=spec,
                idempotency_key=idempotency_key,
            )
        return _request_response(result)


def _request_response(result: RequestAdmissionResult) -> ToolResponse:
    """Map service-level request admission output to the MCP response envelope."""
    if result.error is not None:
        return ToolResponse.failure_from_error(result.object_id, result.error)
    if result.resource is None:
        return ToolResponse.failure(
            result.object_id,
            result.category or ErrorCategory.CONFIGURATION_ERROR,
            data={},
        )
    if result.allocation is not None:
        return _grant_or_enqueue_response(result.resource, result.project, result.allocation)
    if result.denial is not None:
        return _denial_response(result.resource.id, result.project, result.denial)
    return ToolResponse.failure(result.object_id, ErrorCategory.INFRASTRUCTURE_FAILURE)


def _grant_or_enqueue_response(
    resource: Resource, project: str, allocation: Allocation
) -> ToolResponse:
    """Render a grant or a queued-enqueue success (ADR-0069).

    A grant carries the chosen host's ``resource_id`` and reports ``granted``; a queued
    ``requested`` allocation reports ``requested`` and carries no ``resource_id`` (it holds
    only a queue position, not a host).
    """
    data = {"project": project}
    if allocation.state is not AllocationState.REQUESTED:
        data["resource_id"] = str(resource.id)
    return ToolResponse.success(
        str(allocation.id),
        allocation.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data=data,
    )


def _denial_response(resource_id: UUID, project: str, outcome: AdmissionOutcome) -> ToolResponse:
    """Map a denial outcome to its typed failure envelope (category-specific)."""
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data = denial_details(outcome)
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
        require_role(ctx, alloc.project, Role.VIEWER)
        return _envelope_for_allocation(alloc)


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
        outcome = await release_with_backstops(
            pool, uid, project=alloc.project, audit_writer=ctx_audit_writer(ctx)
        )
        return _release_response(uid, outcome)


def _release_response(uid: UUID, outcome: ReleaseOutcome) -> ToolResponse:
    """Map release service outcome to the allocations MCP envelope."""
    if outcome.released:
        return ToolResponse.success(str(uid), "released")
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    category = outcome.category or ErrorCategory.CONFIGURATION_ERROR
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"]
        if category is ErrorCategory.STALE_HANDLE
        else [],
        data=data,
    )


async def renew_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    *,
    extend: object,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Extend an allocation's lease window, re-charged and re-checked (ADR-0036 §3).

    Resolves the allocation, requires ``operator`` on its project, and runs renew
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
    data: dict[str, Any] = dict(outcome.details)
    if outcome.current_status:
        data["current_status"] = outcome.current_status
    return ToolResponse.failure(
        str(uid),
        category,
        suggested_next_actions=["allocations.get"],
        data=data,
    )


async def list_allocations(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit: int
) -> ToolResponse:
    """Return the newest allocations for ``project`` in one collection envelope."""
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
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
        return ToolResponse.collection(
            "allocations",
            "ok",
            responses,
            suggested_next_actions=["allocations.get", "allocations.release"],
            data={"project": project},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `allocations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="allocations.request",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_request(
        project: Annotated[str, Field(description="Project to admit the allocation for.")],
        request: Annotated[
            dict[str, Any],
            Field(description="Allocation request payload: size, lease window, resource selector."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior grant."),
        ] = None,
    ) -> ToolResponse:
        """Admit an allocation against project budget, quota, and host cap. Requires operator."""
        try:
            payload = AllocationRequestPayload.model_validate(request)
        except ValueError:
            return _config_error(project)
        return await request_allocation(
            pool,
            current_context(),
            project=project,
            request=payload,
            idempotency_key=idempotency_key,
        )

    @app.tool(
        name="allocations.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_get(
        allocation_id: Annotated[str, Field(description="The Allocation to render.")],
    ) -> ToolResponse:
        """Render an Allocation; failed maps to a failure envelope. Requires viewer."""
        return await get_allocation(pool, current_context(), allocation_id)

    @app.tool(
        name="allocations.release",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_release(
        allocation_id: Annotated[str, Field(description="The Allocation to release.")],
    ) -> ToolResponse:
        """Drive an Allocation to released and reconcile its spend. Requires operator."""
        return await release_allocation(pool, current_context(), allocation_id)

    @app.tool(
        name="allocations.renew",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def allocations_renew(
        allocation_id: Annotated[str, Field(description="The Allocation to renew.")],
        extend: Annotated[
            float | str,
            Field(description="Additional hours to add (number or decimal string, > 0)."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior renewal."),
        ] = None,
    ) -> ToolResponse:
        """Extend an Allocation's lease window, re-charged and re-checked. Requires operator."""
        return await renew_allocation(
            pool,
            current_context(),
            allocation_id,
            extend=extend,
            idempotency_key=idempotency_key,
        )

    @app.tool(
        name="allocations.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_list(
        project: Annotated[str, Field(description="Project whose allocations to list.")],
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """List the newest Allocations for a project. Requires viewer."""
        return await list_allocations(pool, current_context(), project=project, limit=limit)
