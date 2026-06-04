"""The `accounting.*` MCP tools — the cost/usage surface (ADR-0007).

M1 ships ``accounting.estimate``: a pure read-side price of a hypothetical selector and
window, with **no** allocation, ledger, or budget row touched (those land with the
metering/admission issues). Thin FastMCP wrappers over a plain async handler (pool + ctx
injected; tested directly). RBAC: ``estimate`` requires ``viewer`` on the target project
(the read-side floor); a malformed selector/window or a missing coefficient fails closed
(``configuration_error``), so a returned estimate is always ``≥ 0`` (ADR-0007 §2).
"""

from __future__ import annotations

from decimal import Decimal

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.domain.cost import (
    W_CPU,
    W_MEM,
    Selector,
    cost,
    parse_window_hours,
    quantize_kcu,
    rate,
    resolve_coeff,
    validate_size,
    validate_window,
)
from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security.rbac import Role, require_role

_ESTIMATE_OBJECT_ID = "estimate"
_DEFAULT_COST_CLASS = "local"


async def estimate(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    vcpus: int,
    memory_gb: int,
    window: object,
    cost_class: str = _DEFAULT_COST_CLASS,
) -> ToolResponse:
    """Price a hypothetical ``selector`` over ``window`` hours, without writing anything.

    Validates the selector and window first (fail-closed → ``configuration_error``, so
    the estimate is never negative), resolves the coefficient (missing →
    ``configuration_error``), then returns ``rate × window_hours`` with a vcpu/memory
    breakdown. Requires ``viewer`` on ``project``.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    with bind_context(principal=ctx.principal):
        try:
            return await _estimate_inner(
                pool,
                project=project,
                vcpus=vcpus,
                memory_gb=memory_gb,
                window=window,
                cost_class=cost_class,
            )
        except CategorizedError as exc:
            return ToolResponse.failure(
                _ESTIMATE_OBJECT_ID,
                exc.category,
                suggested_next_actions=["accounting.estimate"],
            )


async def _estimate_inner(
    pool: AsyncConnectionPool,
    *,
    project: str,
    vcpus: int,
    memory_gb: int,
    window: object,
    cost_class: str,
) -> ToolResponse:
    selector = Selector(vcpus=vcpus, memory_gb=memory_gb, cost_class=cost_class)
    validate_size(selector)
    window_hours = parse_window_hours(window)
    validate_window(window_hours)
    async with pool.connection() as conn:
        coeff = await resolve_coeff(conn, selector.cost_class)
    return _estimate_response(coeff, selector, window_hours, project=project)


def _estimate_response(
    coeff: Decimal, selector: Selector, window_hours: Decimal, *, project: str
) -> ToolResponse:
    rate_kcu_per_hr = rate(coeff, vcpus=selector.vcpus, memory_gb=selector.memory_gb)
    estimate_kcu = cost(rate_kcu_per_hr, window_hours)
    vcpu_component = coeff * W_CPU * selector.vcpus
    memory_component = coeff * W_MEM * selector.memory_gb
    return ToolResponse.success(
        _ESTIMATE_OBJECT_ID,
        "ok",
        suggested_next_actions=["allocations.request"],
        data={
            "project": project,
            "cost_class": selector.cost_class,
            "estimate_kcu": str(quantize_kcu(estimate_kcu)),
            "rate_kcu_per_hr": str(quantize_kcu(rate_kcu_per_hr)),
            "breakdown_vcpu_kcu_per_hr": str(quantize_kcu(vcpu_component)),
            "breakdown_memory_kcu_per_hr": str(quantize_kcu(memory_component)),
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `accounting.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="accounting.estimate")
    async def accounting_estimate(
        project: str,
        vcpus: int,
        memory_gb: int,
        window: float,
        cost_class: str = _DEFAULT_COST_CLASS,
    ) -> ToolResponse:
        return await estimate(
            pool,
            current_context(),
            project=project,
            vcpus=vcpus,
            memory_gb=memory_gb,
            window=window,
            cost_class=cost_class,
        )
