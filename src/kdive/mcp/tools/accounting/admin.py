"""Accounting budget and quota administration MCP tools (ADR-0007)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Budget, Quota
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security import audit
from kdive.security.context import RequestContext, require_project
from kdive.security.rbac import Role, require_role

_BUDGET_OBJECT_ID = "budget"
_QUOTA_OBJECT_ID = "quota"
# A deterministic placeholder for natural-keyed accounting rows, which have no UUID.
_ACCOUNTING_AUDIT_ID = UUID(int=0)


async def set_budget(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit_kcu: object
) -> ToolResponse:
    """Set a project's spend budget ``limit_kcu`` (admin; re-set preserves ``spent_kcu``)."""
    require_project(ctx, project)
    require_role(ctx, project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        try:
            limit = _parse_non_negative_kcu(limit_kcu)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _BUDGET_OBJECT_ID, exc.category, suggested_next_actions=["accounting.set_budget"]
            )
        now = datetime.now(UTC)
        async with pool.connection() as conn, conn.transaction():
            await BUDGETS.upsert(
                conn,
                Budget(project=project, limit_kcu=limit, spent_kcu=Decimal(0), updated_at=now),
            )
            await _audit_set(conn, ctx, project, "set_budget", {"limit_kcu": str(limit)})
            return ToolResponse.success(
                _BUDGET_OBJECT_ID,
                "ok",
                suggested_next_actions=["accounting.usage_project", "allocations.request"],
                data={"project": project, "limit_kcu": str(limit)},
            )


async def set_quota(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> ToolResponse:
    """Set a project's two concurrency caps (admin; ADR-0007 §4,6)."""
    require_project(ctx, project)
    require_role(ctx, project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        if max_concurrent_allocations < 0 or max_concurrent_systems < 0:
            return ToolResponse.failure(
                _QUOTA_OBJECT_ID,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=["accounting.set_quota"],
            )
        now = datetime.now(UTC)
        async with pool.connection() as conn, conn.transaction():
            await QUOTAS.upsert(
                conn,
                Quota(
                    project=project,
                    max_concurrent_allocations=max_concurrent_allocations,
                    max_concurrent_systems=max_concurrent_systems,
                    updated_at=now,
                ),
            )
            await _audit_set(
                conn,
                ctx,
                project,
                "set_quota",
                {
                    "max_concurrent_allocations": str(max_concurrent_allocations),
                    "max_concurrent_systems": str(max_concurrent_systems),
                },
            )
            return ToolResponse.success(
                _QUOTA_OBJECT_ID,
                "ok",
                suggested_next_actions=["accounting.usage_project", "allocations.request"],
                data={
                    "project": project,
                    "max_concurrent_allocations": str(max_concurrent_allocations),
                    "max_concurrent_systems": str(max_concurrent_systems),
                },
            )


def _parse_non_negative_kcu(value: object) -> Decimal:
    """Parse ``value`` into a finite, non-negative kcu Decimal."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, DecimalException, ValueError, TypeError):
        raise CategorizedError(
            f"limit_kcu {value!r} is not a number", category=ErrorCategory.CONFIGURATION_ERROR
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise CategorizedError(
            f"limit_kcu {value!r} must be a finite number >= 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed


async def _audit_set(
    conn: AsyncConnection, ctx: RequestContext, project: str, tool: str, values: dict[str, str]
) -> None:
    """Audit an admin set-op under the nil UUID, carrying the project and values in args."""
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=f"accounting.{tool}",
            object_kind="budgets" if tool == "set_budget" else "quotas",
            object_id=_ACCOUNTING_AUDIT_ID,
            transition=f"{tool}:applied",
            args={"project": project, **values},
            project=project,
        ),
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register accounting administration tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.set_budget",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def accounting_set_budget(
        project: Annotated[str, Field(description="Project to set the spend budget for.")],
        limit_kcu: Annotated[
            float | str,
            Field(description="Budget ceiling in KCU (number or decimal string, >= 0)."),
        ],
    ) -> ToolResponse:
        """Set a project's spend budget limit_kcu; preserves spent_kcu. Requires admin."""
        return await set_budget(pool, current_context(), project=project, limit_kcu=limit_kcu)

    @app.tool(
        name="accounting.set_quota",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def accounting_set_quota(
        project: Annotated[str, Field(description="Project to set concurrency caps for.")],
        max_concurrent_allocations: Annotated[
            int, Field(description="Maximum concurrent allocations allowed (>= 0).")
        ],
        max_concurrent_systems: Annotated[
            int, Field(description="Maximum concurrent Systems allowed (>= 0).")
        ],
    ) -> ToolResponse:
        """Set a project's concurrency caps for allocations and systems. Requires admin."""
        return await set_quota(
            pool,
            current_context(),
            project=project,
            max_concurrent_allocations=max_concurrent_allocations,
            max_concurrent_systems=max_concurrent_systems,
        )
