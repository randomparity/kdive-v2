"""Accounting report MCP tools (ADR-0043)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.mcp.tools._time_window import parse_timestamptz_window
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)
from kdive.services.accounting import ledger as accounting_domain

_REPORT_OBJECT_ID = "report"
_REPORT_GRANTED_SET_TOOL = "accounting.report_granted_set"
_REPORT_ALL_PROJECTS_TOOL = "accounting.report_all_projects"
_SCOPE_GRANTED_SET = "granted-set"
_GROUP_BY_PRINCIPAL = "principal"


async def report_granted_set(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    projects: list[str] | None = None,
    group_by: str | None = None,
    window: object = None,
) -> ToolResponse:
    """Roll up caller-authorized member projects (ADR-0043 §3)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_group_by = _parse_group_by(group_by)
            parsed_window = _parse_window(window)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID,
                exc,
                suggested_next_actions=[_REPORT_GRANTED_SET_TOOL],
            )
        return await _report_granted_set(pool, ctx, projects, parsed_group_by, parsed_window)


async def report_all_projects(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    group_by: str | None = None,
    window: object = None,
) -> ToolResponse:
    """Roll up all projects under the platform-auditor role (ADR-0043 §3)."""
    with bind_context(principal=ctx.principal):
        try:
            parsed_group_by = _parse_group_by(group_by)
            parsed_window = _parse_window(window)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _REPORT_OBJECT_ID,
                exc,
                suggested_next_actions=[_REPORT_ALL_PROJECTS_TOOL],
            )
        return await _report_all_projects(pool, ctx, parsed_group_by, parsed_window)


async def _report_granted_set(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    named: list[str] | None,
    group_by: Literal["principal"] | None,
    window: tuple[datetime | None, datetime | None] | None,
) -> ToolResponse:
    """Resolve + authorize the member project set, roll up, audit by read-shape."""
    targets = _resolve_granted_set(ctx, named)
    async with pool.connection() as conn:
        rollup = await accounting_domain.report(
            conn, projects=targets, group_by=group_by, window=window
        )
        if _audit_granted_set(targets, group_by):
            scope_value = f"{_SCOPE_GRANTED_SET}:{','.join(sorted(targets))}"
            async with conn.transaction():
                await audit.record_platform(
                    conn,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    event=audit.PlatformAuditEvent(
                        tool=_REPORT_GRANTED_SET_TOOL,
                        scope=scope_value,
                        args=_report_args(_SCOPE_GRANTED_SET, named, group_by, window),
                        platform_role=None,
                        actor=actor_for(ctx),
                    ),
                )
    return _report_response(_SCOPE_GRANTED_SET, group_by, targets, rollup)


def _resolve_granted_set(ctx: RequestContext, named: list[str] | None) -> list[str]:
    """Return the authorized target projects (default → member-with-role; named → checked).

    The default set (no ``named``) is the projects in ``ctx.projects`` with a non-None role
    (``viewer`` is rank 0, so "has a role" already satisfies the floor); role-less
    memberships are dropped. A **named** set authorizes each via ``require_role(viewer)``,
    which raises ``AuthorizationError`` for a non-member or role-less project.
    """
    if named is None:
        return [p for p in ctx.projects if ctx.roles.get(p) is not None]
    for project in named:
        require_role(ctx, project, Role.VIEWER)
    return list(named)


def _audit_granted_set(targets: list[str], group_by: str | None) -> bool:
    """A granted-set read is audited iff it spans >1 project or groups by principal."""
    return len(targets) > 1 or group_by == _GROUP_BY_PRINCIPAL


async def _report_all_projects(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    group_by: Literal["principal"] | None,
    window: tuple[datetime | None, datetime | None] | None,
) -> ToolResponse:
    """Gate ``platform_auditor``, roll up every project, always audit (denials too)."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    except AuthorizationError:
        await _audit_all_projects_denial(pool, ctx, group_by, window)
        return ToolResponse.failure(
            _REPORT_OBJECT_ID,
            ErrorCategory.AUTHORIZATION_DENIED,
            suggested_next_actions=[_REPORT_ALL_PROJECTS_TOOL],
        )
    async with pool.connection() as conn:
        targets = await _all_projects(conn)
        rollup = await accounting_domain.report(
            conn, projects=targets, group_by=group_by, window=window
        )
        async with conn.transaction():
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_REPORT_ALL_PROJECTS_TOOL,
                    scope=ALL_PROJECTS_SCOPE,
                    args=_report_args(ALL_PROJECTS_SCOPE, None, group_by, window),
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
    return _report_response(ALL_PROJECTS_SCOPE, group_by, targets, rollup)


async def _audit_all_projects_denial(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    group_by: Literal["principal"] | None,
    window: tuple[datetime | None, datetime | None] | None,
) -> None:
    """Audit an all-projects denial iff the caller holds ≥1 platform role (ADR-0043 §4).

    A project-only token's denial is the routine non-grant case and is *not* recorded —
    auditing it would let any authenticated token amplify writes into ``platform_audit_log``
    on this openly-callable read. The role check runs before any pool connection is open, so
    the denial-audit opens its own connection and transaction here.
    """
    await audit_platform_denial(
        pool,
        ctx,
        tool=_REPORT_ALL_PROJECTS_TOOL,
        scope=ALL_PROJECTS_SCOPE,
        args=_report_args(ALL_PROJECTS_SCOPE, None, group_by, window),
    )


async def _all_projects(conn: AsyncConnection) -> list[str]:
    """The project universe for the all-projects form: every project with spend or a budget.

    The oversight read must span *every* project (ADR-0043 §3), so the universe unions the
    ``ledger`` and ``budgets`` projects — a project that has ledger spend but no (or a
    removed) budget row is still reported, rather than silently dropped from the cross-tenant
    total. A budgeted project with no spend yet still appears (it contributes an empty
    rollup, so no row, but the set is honest about what was considered).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT project FROM ledger UNION SELECT project FROM budgets ORDER BY project"
        )
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]


def _parse_group_by(group_by: str | None) -> Literal["principal"] | None:
    if group_by is None:
        return None
    if group_by == _GROUP_BY_PRINCIPAL:
        return "principal"
    raise CategorizedError(
        f"group_by {group_by!r} is not supported (only 'principal')",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": "group_by", "value": group_by},
    )


def _parse_window(window: object) -> tuple[datetime | None, datetime | None] | None:
    """Parse ``window`` into a ``(start, end)`` datetime pair, or ``None`` for all time.

    ``window`` is a two-element ``[start, end]`` of **timezone-aware** ISO-8601 strings
    (either may be ``None``), or ``None``. Fails closed (``configuration_error``) on a
    non-pair, an unparseable or tz-naive bound, or a non-ordered ``start >= end`` range —
    so a malformed window surfaces an error rather than a silently-empty rollup. ``ledger.ts``
    is ``timestamptz``; a tz-naive bound would compare in an unintended zone.
    """
    return parse_timestamptz_window(window, timestamp_column="ledger.ts")


def _report_args(
    scope: str,
    named: list[str] | None,
    group_by: str | None,
    window: tuple[datetime | None, datetime | None] | None,
) -> dict[str, object]:
    """The public tool args, for the audit ``args_digest`` (no secret values)."""
    return {
        "scope": scope,
        "projects": named,
        "group_by": group_by,
        "window": [w.isoformat() if w else None for w in window] if window else None,
    }


def _rollup_row_data(row: accounting_domain.RollupRow) -> dict[str, str]:
    return {
        "project": row.project,
        "principal": row.principal or "",
        "reserved": str(row.reserved),
        "reconciled": str(row.reconciled),
        "variance": str(row.variance),
    }


def _report_response(
    scope: str, group_by: str | None, targets: list[str], rollup: accounting_domain.Report
) -> ToolResponse:
    items = [
        ToolResponse.success(_rollup_object_id(row), "ok", data=_rollup_row_data(row))
        for row in rollup.rows
    ]
    total = _rollup_row_data(rollup.total)
    return ToolResponse.collection(
        _REPORT_OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=["accounting.usage_project"],
        data={
            "scope": scope,
            "group_by": group_by or "",
            "project_count": str(len(targets)),
            "total_project": total["project"],
            "total_principal": total["principal"],
            "total_reserved": total["reserved"],
            "total_reconciled": total["reconciled"],
            "total_variance": total["variance"],
        },
    )


def _rollup_object_id(row: accounting_domain.RollupRow) -> str:
    if row.principal is None:
        return row.project
    return f"{row.project}:{row.principal}"


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register report accounting tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.report_granted_set",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_report_granted_set(
        projects: Annotated[
            list[str] | None,
            Field(description="Named project subset for granted-set scope; omit for all members."),
        ] = None,
        group_by: Annotated[
            str | None,
            Field(description="Group rows by 'principal', or omit for per-project grouping."),
        ] = None,
        window: Annotated[
            list[str | None] | None,
            Field(description="[start, end] ISO-8601 timestamptz pair; omit for all time."),
        ] = None,
    ) -> ToolResponse:
        """Multi-project usage rollup over caller-authorized projects. Requires viewer."""
        return await report_granted_set(
            pool, current_context(), projects=projects, group_by=group_by, window=window
        )

    @app.tool(
        name="accounting.report_all_projects",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_report_all_projects(
        group_by: Annotated[
            str | None,
            Field(description="Group rows by 'principal', or omit for per-project grouping."),
        ] = None,
        window: Annotated[
            list[str | None] | None,
            Field(description="[start, end] ISO-8601 timestamptz pair; omit for all time."),
        ] = None,
    ) -> ToolResponse:
        """Multi-project usage rollup over every project. Requires platform auditor."""
        return await report_all_projects(pool, current_context(), group_by=group_by, window=window)
