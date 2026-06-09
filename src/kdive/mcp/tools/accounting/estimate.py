"""Read-side accounting estimate MCP tool (ADR-0007)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

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
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import EstimateRequestPayload
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role

_ESTIMATE_OBJECT_ID = "estimate"


async def estimate(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    request: EstimateRequestPayload,
) -> ToolResponse:
    """Price a hypothetical ``selector`` over ``window`` hours, without writing anything."""
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    with bind_context(principal=ctx.principal):
        try:
            return await _estimate_inner(
                pool,
                project=project,
                request=request,
            )
        except ValueError:
            return ToolResponse.failure(
                _ESTIMATE_OBJECT_ID,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=["accounting.estimate"],
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _ESTIMATE_OBJECT_ID,
                exc,
                suggested_next_actions=["accounting.estimate"],
            )


async def _estimate_inner(
    pool: AsyncConnectionPool,
    *,
    project: str,
    request: EstimateRequestPayload,
) -> ToolResponse:
    selector = Selector(
        vcpus=request.vcpus, memory_gb=request.memory_gb, cost_class=request.cost_class
    )
    validate_size(selector)
    window_hours = parse_window_hours(request.window)
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
    """Register ``accounting.estimate`` on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.estimate",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_estimate(
        project: Annotated[str, Field(description="Project to price the estimate for.")],
        request: Annotated[
            dict[str, Any],
            Field(description="Estimate request payload: size, lease window, cost class."),
        ],
    ) -> ToolResponse:
        """Price a hypothetical selector over a window without writing anything. Requires viewer."""
        try:
            payload = EstimateRequestPayload.model_validate(request)
        except ValueError:
            return ToolResponse.failure(
                _ESTIMATE_OBJECT_ID,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=["accounting.estimate"],
            )
        return await estimate(
            pool,
            current_context(),
            project=project,
            request=payload,
        )
