"""The ``audit.query`` auditor-read tool (ADR-0062 §6, #141).

Reads ``audit_log`` in two explicit scopes:

* **project** — requires ``project`` and ``require_role(project, admin)``; the audit trail
  is sensitive, so only a project admin reads their own. Not written to
  ``platform_audit_log`` (a member reading their own trail is not a cross-tenant read).
* **all-projects** — forbids ``project`` and requires ``platform_auditor`` (satisfied by
  ``platform_admin``); read-audited to ``platform_audit_log``. The read target is
  ``audit_log`` while the read-access record lands in ``platform_audit_log``, so a platform
  read never pollutes the per-project trail it inspects (ADR-0043 §4).

Filterable by principal / object / time window / transition. Thin FastMCP wrappers over a
plain async handler taking the pool + request context (tested directly, never through MCP).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, LiteralString
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops import _reads
from kdive.mcp.tools.ops._auth import ALL_PROJECTS_SCOPE
from kdive.security.context import RequestContext
from kdive.security.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    RoleDenied,
    require_platform_role,
    require_role,
)

_TOOL = "audit.query"
_OBJECT_ID = "audit.query"
_MAX_ROWS = 500
type AuditQueryScope = Literal["project", "all-projects"]
_PROJECT_SCOPE = "project"


async def query(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    scope: AuditQueryScope | None = None,
    project: str | None = None,
    principal: str | None = None,
    object_id: str | None = None,
    transition: str | None = None,
    window: object = None,
) -> ToolResponse:
    """Read ``audit_log`` with explicit project or all-projects scope."""
    with bind_context(principal=ctx.principal):
        try:
            object_uuid = _parse_object_id(object_id)
            parsed_window = _reads.parse_window(window)
        except CategorizedError as exc:
            return ToolResponse.failure(_OBJECT_ID, exc.category, suggested_next_actions=[_TOOL])
        filters = _Filters(principal, object_uuid, transition, parsed_window)
        scope_error = _scope_error(scope, project)
        if scope_error is not None:
            return scope_error
        if scope == _PROJECT_SCOPE:
            assert project is not None
            return await _query_project(pool, ctx, project, filters)
        assert scope == ALL_PROJECTS_SCOPE
        return await _query_cross_project(pool, ctx, filters)


class _Filters:
    """The four optional row filters, validated and ready to bind into the SQL."""

    __slots__ = ("object_id", "principal", "transition", "window")

    def __init__(
        self,
        principal: str | None,
        object_id: UUID | None,
        transition: str | None,
        window: tuple[datetime | None, datetime | None] | None,
    ) -> None:
        self.principal = principal
        self.object_id = object_id
        self.transition = transition
        self.window = window


def _parse_object_id(object_id: str | None) -> UUID | None:
    if object_id is None:
        return None
    try:
        return UUID(object_id)
    except ValueError:
        raise CategorizedError(
            f"object_id {object_id!r} is not a uuid",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


def _scope_error(scope: str | None, project: str | None) -> ToolResponse | None:
    if scope is None:
        return _config_failure("scope_required")
    if scope == _PROJECT_SCOPE:
        if project is None:
            return _config_failure("project_required")
        return None
    if scope == ALL_PROJECTS_SCOPE:
        if project is not None:
            return _config_failure("project_not_allowed")
        return None
    return _config_failure("unknown_scope")


def _config_failure(reason: str) -> ToolResponse:
    return ToolResponse.failure(
        _OBJECT_ID,
        ErrorCategory.CONFIGURATION_ERROR,
        suggested_next_actions=[_TOOL],
        data={"reason": reason},
    )


async def _query_project(
    pool: AsyncConnectionPool, ctx: RequestContext, project: str, filters: _Filters
) -> ToolResponse:
    """Project-scoped form: require admin on ``project``, read only its rows, no platform audit."""
    try:
        require_role(ctx, project, Role.ADMIN)
    except RoleDenied:
        # A member's rank-below over-reach must reach the dispatch boundary so
        # DenialAuditMiddleware records the denial (ADR-0062 §5); do not swallow it
        # into a failure envelope here. The non-member base AuthorizationError is not
        # boundary-audited, so it keeps the graceful envelope below.
        raise
    except AuthorizationError:
        return ToolResponse.failure(
            _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
        )
    async with pool.connection() as conn:
        rows = await _fetch_rows(conn, project=project, filters=filters)
    return _response(rows)


async def _query_cross_project(
    pool: AsyncConnectionPool, ctx: RequestContext, filters: _Filters
) -> ToolResponse:
    """Cross-project form: require ``platform_auditor``, read all projects, read-audit."""
    args = _audit_args(filters)
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    except AuthorizationError:
        await _reads.audit_denial(pool, ctx, tool=_TOOL, args=args)
        return ToolResponse.failure(
            _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
        )
    async with pool.connection() as conn:
        rows = await _fetch_rows(conn, project=None, filters=filters)
        await _reads.record_read(conn, ctx, tool=_TOOL, args=args)
    return _response(rows)


async def _fetch_rows(
    conn: AsyncConnection, *, project: str | None, filters: _Filters
) -> list[dict[str, object]]:
    """Read filtered ``audit_log`` rows.

    The WHERE clause is assembled from a fixed set of **literal** fragments (so the query
    stays a ``LiteralString`` — no runtime-string interpolation reaches the SQL); every
    filter value is bound as a ``%s`` parameter.
    """
    params: list[object] = []
    where: LiteralString = ""
    if project is not None:
        where += " AND project = %s"
        params.append(project)
    if filters.principal is not None:
        where += " AND principal = %s"
        params.append(filters.principal)
    if filters.object_id is not None:
        where += " AND object_id = %s"
        params.append(filters.object_id)
    if filters.transition is not None:
        where += " AND transition = %s"
        params.append(filters.transition)
    if filters.window is not None:
        start, end = filters.window
        if start is not None:
            where += " AND ts >= %s"
            params.append(start)
        if end is not None:
            where += " AND ts < %s"
            params.append(end)
    query: LiteralString = (
        "SELECT ts, principal, agent_session, project, tool, object_kind, object_id, "
        "transition FROM audit_log WHERE true" + where + " ORDER BY ts DESC, id DESC LIMIT %s"
    )
    params.append(_MAX_ROWS)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


def _audit_args(filters: _Filters) -> dict[str, object]:
    """The public filter args for the audit ``args_digest`` (no secret values)."""
    window = filters.window
    return {
        "scope": ALL_PROJECTS_SCOPE,
        "principal": filters.principal,
        "object_id": str(filters.object_id) if filters.object_id is not None else None,
        "transition": filters.transition,
        "window": [w.isoformat() if w else None for w in window] if window else None,
    }


def _row_json(row: dict[str, object]) -> dict[str, str | None]:
    ts = row["ts"]
    object_id = row["object_id"]
    return {
        "ts": ts.isoformat() if isinstance(ts, datetime) else str(ts),
        "principal": _as_str(row["principal"]),
        "agent_session": _as_str(row["agent_session"]),
        "project": _as_str(row["project"]),
        "tool": _as_str(row["tool"]),
        "object_kind": _as_str(row["object_kind"]),
        "object_id": str(object_id) if object_id is not None else None,
        "transition": _as_str(row["transition"]),
    }


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)


def _response(rows: list[dict[str, object]]) -> ToolResponse:
    return ToolResponse.success(
        _OBJECT_ID,
        "ok",
        suggested_next_actions=["inventory.list"],
        data={
            "count": str(len(rows)),
            "truncated": "true" if len(rows) >= _MAX_ROWS else "false",
            "rows": json.dumps([_row_json(r) for r in rows]),
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``audit.query`` tool on ``app``, bound to ``pool``."""

    @app.tool(
        name="audit.query",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def audit_query(
        scope: Annotated[
            AuditQueryScope,
            Field(description="'project' for one project, or 'all-projects' for platform audit."),
        ],
        project: Annotated[
            str | None,
            Field(description="Project to read when scope is 'project'; forbidden otherwise."),
        ] = None,
        principal: Annotated[str | None, Field(description="Filter by acting principal.")] = None,
        object_id: Annotated[
            str | None, Field(description="Filter by audited object UUID.")
        ] = None,
        transition: Annotated[
            str | None, Field(description="Filter by transition literal (e.g. 'requested').")
        ] = None,
        window: Annotated[
            list[str | None] | None,
            Field(description="[start, end] ISO-8601 timestamptz pair; omit for all time."),
        ] = None,
    ) -> ToolResponse:
        """Read audit_log: project form (admin) or cross-project (platform_auditor).

        Returns the most recent matching rows (capped at 500, newest first);
        ``data.truncated`` is "true" when the cap is hit — narrow with filters.
        """
        return await query(
            pool,
            current_context(),
            scope=scope,
            project=project,
            principal=principal,
            object_id=object_id,
            transition=transition,
            window=window,
        )
