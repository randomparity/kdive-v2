"""Read-only `systems.*` MCP handlers (ADR-0025, ADR-0070)."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import System
from kdive.domain.pcie import parse_match_spec
from kdive.domain.state import SystemState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
CUSTOM_SHAPE_SENTINEL = "__custom__"
"""The ``shape`` filter value selecting full-custom Systems (``shape IS NULL``)."""


def system_envelope(system: System) -> ToolResponse:
    """Render a System; ``failed`` becomes a failure envelope."""
    if system.state is SystemState.FAILED:
        return ToolResponse.failure(
            str(system.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": system.state.value},
        )
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data={"project": system.project},
    )


def defined_system_envelope(system: System) -> ToolResponse:
    """Render a newly defined System with its upload/provision next actions."""
    return ToolResponse.success(
        str(system.id),
        SystemState.DEFINED.value,
        suggested_next_actions=["artifacts.create_system_upload", "systems.provision_defined"],
        data={"project": system.project},
    )


async def get_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Return a System the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
        if system is None or system.project not in ctx.projects:
            return _config_error(system_id)
        require_role(ctx, system.project, Role.VIEWER)
        return system_envelope(system)


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


@dataclass(frozen=True, slots=True)
class _SystemFilters:
    """The validated, SQL-ready clauses and params for a :func:`list_systems` query."""

    clauses: list[Composable]
    params: list[object]


def _build_filters(
    viewer_projects: list[str],
    *,
    allocation_id: str | None,
    state: str | None,
    shape: str | None,
    pcie: str | None,
) -> _SystemFilters | ToolResponse:
    """Translate filter args into SQL clauses, or a ``configuration_error`` envelope."""
    clauses: list[Composable] = [sql.SQL("s.project = ANY(%s)")]
    params: list[object] = [viewer_projects]
    if allocation_id is not None:
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        clauses.append(sql.SQL("s.allocation_id = %s"))
        params.append(uid)
    if state is not None:
        try:
            resolved = SystemState(state)
        except ValueError:
            return _config_error(state)
        clauses.append(sql.SQL("s.state = %s"))
        params.append(resolved.value)
    if shape is not None:
        if shape == CUSTOM_SHAPE_SENTINEL:
            clauses.append(sql.SQL("s.shape IS NULL"))
        else:
            clauses.append(sql.SQL("s.shape = %s"))
            params.append(shape)
    if pcie is not None:
        pcie_clause = _pcie_clause(pcie, params)
        if isinstance(pcie_clause, ToolResponse):
            return pcie_clause
        clauses.append(pcie_clause)
    return _SystemFilters(clauses, params)


def _pcie_clause(pcie: str, params: list[object]) -> Composable | ToolResponse:
    """Build the ``pcie`` SQL predicate, or a ``configuration_error`` envelope."""
    try:
        spec = parse_match_spec(pcie.strip())
    except CategorizedError as exc:
        return ToolResponse.failure(pcie, exc.category)
    if spec.vendor_id is None or spec.device_id is None:
        return _config_error(pcie)
    params.extend([spec.vendor_id, spec.device_id])
    return sql.SQL(
        "EXISTS (SELECT 1 FROM jsonb_array_elements(a.pcie_claim) e "
        "WHERE e->>'vendor_id' = %s AND e->>'device_id' = %s)"
    )


async def list_systems(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str | None = None,
    state: str | None = None,
    shape: str | None = None,
    pcie: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> ToolResponse:
    """List the caller's Systems, filterable by allocation, state, shape, and PCIe match."""
    viewer_projects = _viewer_projects(ctx)
    filters = _build_filters(
        viewer_projects, allocation_id=allocation_id, state=state, shape=shape, pcie=pcie
    )
    if isinstance(filters, ToolResponse):
        return filters
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    with bind_context(principal=ctx.principal):
        if not viewer_projects:
            return _systems_collection([])
        query = sql.SQL(
            "SELECT s.* FROM systems s JOIN allocations a ON a.id = s.allocation_id "
            "WHERE {where} ORDER BY s.created_at DESC, s.id LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(filters.clauses))
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (*filters.params, capped))
            rows = await cur.fetchall()
        return _systems_collection([System.model_validate(row) for row in rows])


def _systems_collection(systems: list[System]) -> ToolResponse:
    """Render Systems into one collection envelope."""
    return ToolResponse.collection(
        "systems",
        "ok",
        [system_envelope(system) for system in systems],
        suggested_next_actions=["systems.get", "runs.create"],
    )
