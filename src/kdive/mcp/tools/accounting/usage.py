"""Accounting usage MCP tools (ADR-0007 §6)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.services.accounting import ledger as accounting_domain

_USAGE_OBJECT_ID = "usage"


async def usage_project(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
) -> ToolResponse:
    """Report a project's spend rollup; ``viewer`` of the target project (ADR-0007 §6)."""
    with bind_context(principal=ctx.principal):
        try:
            require_project(ctx, project)
            require_role(ctx, project, Role.VIEWER)
            async with pool.connection() as conn:
                rollup = await accounting_domain.usage(conn, project)
            return _usage_response(project, rollup)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _USAGE_OBJECT_ID,
                exc,
                suggested_next_actions=["accounting.usage_project"],
            )


async def usage_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
) -> ToolResponse:
    """Report spend for one investigation plus its owning project rollup."""
    with bind_context(principal=ctx.principal):
        try:
            try:
                inv_uuid = UUID(investigation_id)
            except ValueError:
                raise CategorizedError(
                    f"investigation_id {investigation_id!r} is not a uuid",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"field": "investigation_id", "value": investigation_id},
                ) from None
            async with pool.connection() as conn:
                owning_project = await _resolve_investigation_project(conn, inv_uuid)
                if owning_project is None:
                    raise CategorizedError(
                        f"investigation {investigation_id} does not exist",
                        category=ErrorCategory.CONFIGURATION_ERROR,
                        details={"field": "investigation_id", "value": investigation_id},
                    )
                require_project(ctx, owning_project)
                require_role(ctx, owning_project, Role.VIEWER)
                rollup = await accounting_domain.usage(conn, owning_project)
                investigation_kcu = await accounting_domain.usage_for_investigation(conn, inv_uuid)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _USAGE_OBJECT_ID,
                exc,
                suggested_next_actions=["accounting.usage_investigation"],
            )
    response = _usage_response(owning_project, rollup)
    response.data["investigation_id"] = investigation_id
    response.data["investigation_kcu"] = str(investigation_kcu)
    return response


async def _resolve_investigation_project(conn: AsyncConnection, inv_id: UUID) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT project FROM investigations WHERE id = %s", (inv_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


def _usage_response(project: str, rollup: accounting_domain.ProjectUsage) -> ToolResponse:
    by_cost_class = {cls: str(val) for cls, val in rollup.by_cost_class.items()}
    return ToolResponse.success(
        _USAGE_OBJECT_ID,
        "ok",
        suggested_next_actions=["accounting.estimate", "allocations.list"],
        data={
            "project": project,
            "spent_kcu": str(rollup.spent_kcu),
            "budget_remaining": str(rollup.budget_remaining),
            "shared_kcu": str(rollup.shared_kcu),
            "by_cost_class": by_cost_class,
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register usage accounting tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.usage_project",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_usage_project(
        project: Annotated[str, Field(description="Project to report spend for.")],
    ) -> ToolResponse:
        """Return spend rollup for one project. Requires viewer."""
        return await usage_project(pool, current_context(), project=project)

    @app.tool(
        name="accounting.usage_investigation",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_usage_investigation(
        investigation_id: Annotated[
            str, Field(description="Investigation UUID to report spend for.")
        ],
    ) -> ToolResponse:
        """Return spend rollup for one investigation and its owning project. Requires viewer."""
        return await usage_investigation(pool, current_context(), investigation_id=investigation_id)
