"""The `accounting.*` MCP tools — the cost/usage surface (ADR-0007).

M1 ships ``accounting.estimate``: a pure read-side price of a hypothetical selector and
window, with **no** allocation, ledger, or budget row touched (those land with the
metering/admission issues). Thin FastMCP wrappers over a plain async handler (pool + ctx
injected; tested directly). RBAC: ``estimate`` requires ``viewer`` on the target project
(the read-side floor); a malformed selector/window or a missing coefficient fails closed
(``configuration_error``), so a returned estimate is always ``≥ 0`` (ADR-0007 §2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Literal
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.domain import accounting as accounting_domain
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
from kdive.domain.models import Budget, Quota
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)

_ESTIMATE_OBJECT_ID = "estimate"
_USAGE_OBJECT_ID = "usage"
_BUDGET_OBJECT_ID = "budget"
_QUOTA_OBJECT_ID = "quota"
_REPORT_OBJECT_ID = "report"
_REPORT_TOOL = "accounting.report"
_SCOPE_GRANTED_SET = "granted-set"
_SCOPE_ALL_PROJECTS = "all-projects"
_GROUP_BY_PRINCIPAL = "principal"
_DEFAULT_COST_CLASS = "local"
# A deterministic placeholder for the natural-keyed accounting rows (which have no UUID);
# audit.record's object_id is a UUID, so admin set-ops audit under the nil UUID and carry
# the project in args. The audit args_digest is over project + the set values.
_ACCOUNTING_AUDIT_ID = UUID(int=0)


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


async def usage(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None = None,
    investigation_id: str | None = None,
) -> ToolResponse:
    """Report a project's spend rollup; ``viewer`` of the **target** project (ADR-0007 §6).

    Exactly one of ``project`` / ``investigation_id`` must be set. The ``project`` form
    checks ``require_project`` + ``require_role(viewer)`` on it. The ``investigation_id``
    form first resolves the investigation's owning project, then applies the identical
    check on that project — so a viewer cannot read another project's spend through a
    foreign ``investigation_id`` (the tenant-isolation boundary). The investigation form
    additionally returns that investigation's exclusively-owned rollup.
    """
    with bind_context(principal=ctx.principal):
        try:
            return await _usage_inner(pool, ctx, project=project, investigation_id=investigation_id)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _USAGE_OBJECT_ID,
                exc.category,
                suggested_next_actions=["accounting.usage"],
            )


async def _usage_inner(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str | None,
    investigation_id: str | None,
) -> ToolResponse:
    if (project is None) == (investigation_id is None):
        raise CategorizedError(
            "exactly one of project / investigation_id is required",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if investigation_id is not None:
        return await _usage_for_investigation(pool, ctx, investigation_id)
    assert project is not None  # narrowed by the xor check above
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    async with pool.connection() as conn:
        rollup = await accounting_domain.usage(conn, project)
    return _usage_response(project, rollup)


async def _usage_for_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    try:
        inv_uuid = UUID(investigation_id)
    except ValueError:
        raise CategorizedError(
            f"investigation_id {investigation_id!r} is not a uuid",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    async with pool.connection() as conn:
        owning_project = await _resolve_investigation_project(conn, inv_uuid)
        if owning_project is None:
            raise CategorizedError(
                f"investigation {investigation_id} does not exist",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        # Authorize on the OWNING project — resolved before any spend is read, so a
        # foreign investigation_id cannot leak another tenant's usage.
        require_project(ctx, owning_project)
        require_role(ctx, owning_project, Role.VIEWER)
        rollup = await accounting_domain.usage(conn, owning_project)
        investigation_kcu = await accounting_domain.usage_for_investigation(conn, inv_uuid)
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
            "by_cost_class": json.dumps(by_cost_class, sort_keys=True),
        },
    )


async def report(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    scope: str,
    projects: list[str] | None = None,
    group_by: str | None = None,
    window: object = None,
) -> ToolResponse:
    """Multi-project usage/variance rollup in two scope forms (ADR-0043 §3).

    ``scope="granted-set"`` rolls up the caller's **own** member projects, authorized per
    project by ``require_role(viewer)`` — no platform role. The default set is the projects
    in ``ctx.projects`` carrying a role; a **named** ``projects`` arg authorizes each
    (a non-member or role-less name *raises* ``AuthorizationError``, so a typo surfaces).
    It is read-audited to ``platform_audit_log`` (``platform_role`` null) **iff** the
    authorized set spans >1 project **or** ``group_by="principal"``.

    ``scope="all-projects"`` rolls up every project (``DISTINCT project FROM budgets``),
    gated ``platform_auditor`` (satisfied by ``platform_admin``) and **always**
    read-audited. A denial by a principal holding ≥1 platform role is audited (the held
    role recorded) then mapped to ``authorization_denied``; a denial by a project-only
    token is not audited (the routine non-grant case).

    Both forms return per-project (or per-principal) ``reserved/reconciled/variance`` plus a
    cross-project total. Requires ``viewer`` membership (granted-set) or the platform role
    (all-projects); a malformed ``scope`` / ``group_by`` / ``window`` fails closed.
    """
    with bind_context(principal=ctx.principal):
        try:
            parsed_group_by = _parse_group_by(group_by)
            parsed_window = _parse_window(window)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _REPORT_OBJECT_ID, exc.category, suggested_next_actions=[_REPORT_TOOL]
            )
        if scope == _SCOPE_GRANTED_SET:
            return await _report_granted_set(pool, ctx, projects, parsed_group_by, parsed_window)
        if scope == _SCOPE_ALL_PROJECTS:
            return await _report_all_projects(pool, ctx, parsed_group_by, parsed_window)
        return ToolResponse.failure(
            _REPORT_OBJECT_ID,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_REPORT_TOOL],
        )


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
                    platform_role=None,
                    tool=_REPORT_TOOL,
                    scope=scope_value,
                    args=_report_args(_SCOPE_GRANTED_SET, named, group_by, window),
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
            suggested_next_actions=[_REPORT_TOOL],
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
                platform_role=_held_platform_roles(ctx),
                tool=_REPORT_TOOL,
                scope=_SCOPE_ALL_PROJECTS,
                args=_report_args(_SCOPE_ALL_PROJECTS, None, group_by, window),
            )
    return _report_response(_SCOPE_ALL_PROJECTS, group_by, targets, rollup)


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
    held = _held_platform_roles(ctx)
    if held is None:
        return
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            platform_role=held,
            tool=_REPORT_TOOL,
            scope=_SCOPE_ALL_PROJECTS,
            args=_report_args(_SCOPE_ALL_PROJECTS, None, group_by, window),
        )


def _held_platform_roles(ctx: RequestContext) -> str | None:
    """Return the caller's platform roles as a sorted comma string, or None if it holds none."""
    if not ctx.platform_roles:
        return None
    return ",".join(sorted(r.value for r in ctx.platform_roles))


async def _all_projects(conn: AsyncConnection) -> list[str]:
    """The project universe for the all-projects form: every project with a budget row."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT project FROM budgets ORDER BY project")
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
    )


def _parse_window(window: object) -> tuple[datetime | None, datetime | None] | None:
    """Parse ``window`` into a ``(start, end)`` datetime pair, or ``None`` for all time.

    ``window`` is a two-element ``[start, end]`` of ISO-8601 strings (either may be
    ``None``), or ``None``. A non-pair or unparseable bound fails closed.
    """
    if window is None:
        return None
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise CategorizedError(
            "window must be a [start, end] pair", category=ErrorCategory.CONFIGURATION_ERROR
        )
    start, end = window
    parsed = (_parse_instant(start), _parse_instant(end))
    if parsed == (None, None):
        return None
    return parsed


def _parse_instant(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CategorizedError(
            f"window bound {value!r} is not an ISO-8601 string",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise CategorizedError(
            f"window bound {value!r} is not a valid ISO-8601 timestamp",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


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


def _rollup_row_json(row: accounting_domain.RollupRow) -> dict[str, str | None]:
    return {
        "project": row.project,
        "principal": row.principal,
        "reserved": str(row.reserved),
        "reconciled": str(row.reconciled),
        "variance": str(row.variance),
    }


def _report_response(
    scope: str, group_by: str | None, targets: list[str], rollup: accounting_domain.Report
) -> ToolResponse:
    return ToolResponse.success(
        _REPORT_OBJECT_ID,
        "ok",
        suggested_next_actions=["accounting.usage"],
        data={
            "scope": scope,
            "group_by": group_by or "",
            "projects": json.dumps(sorted(targets)),
            "rows": json.dumps([_rollup_row_json(r) for r in rollup.rows]),
            "total": json.dumps(_rollup_row_json(rollup.total)),
        },
    )


async def set_budget(
    pool: AsyncConnectionPool, ctx: RequestContext, *, project: str, limit_kcu: object
) -> ToolResponse:
    """Set a project's spend budget ``limit_kcu`` (admin; re-set preserves ``spent_kcu``).

    Project administration is ``admin``-only (ADR-0007 §6). The ``limit_kcu`` is parsed
    and validated as a finite ``≥ 0`` number (malformed → ``configuration_error``, no
    write); the upsert updates only ``limit_kcu`` so the DB-maintained ``spent_kcu``
    running total survives a re-set. The write is audited in the same transaction.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        try:
            limit = _parse_non_negative_kcu(limit_kcu)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _BUDGET_OBJECT_ID, exc.category, suggested_next_actions=["accounting.set_budget"]
            )
        now = datetime.now(UTC)  # placeholder; the DB sets updated_at
        async with pool.connection() as conn, conn.transaction():
            await BUDGETS.upsert(
                conn,
                Budget(project=project, limit_kcu=limit, spent_kcu=Decimal(0), updated_at=now),
            )
            await _audit_set(conn, ctx, project, "set_budget", {"limit_kcu": str(limit)})
        return ToolResponse.success(
            _BUDGET_OBJECT_ID,
            "ok",
            suggested_next_actions=["accounting.usage", "allocations.request"],
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
    """Set a project's two concurrency caps (admin; ADR-0007 §4,6).

    Both caps must be ``≥ 0`` (a negative cap is a ``configuration_error``, no write).
    The upsert overwrites both caps; the write is audited in the same transaction.
    Requires ``admin`` on ``project``.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.ADMIN)
    with bind_context(principal=ctx.principal):
        if max_concurrent_allocations < 0 or max_concurrent_systems < 0:
            return ToolResponse.failure(
                _QUOTA_OBJECT_ID,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=["accounting.set_quota"],
            )
        now = datetime.now(UTC)  # placeholder; the DB sets updated_at
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
            suggested_next_actions=["accounting.usage", "allocations.request"],
            data={
                "project": project,
                "max_concurrent_allocations": str(max_concurrent_allocations),
                "max_concurrent_systems": str(max_concurrent_systems),
            },
        )


def _parse_non_negative_kcu(value: object) -> Decimal:
    """Parse ``value`` into a finite, non-negative kcu Decimal (fail closed otherwise).

    Mirrors the ledger's fail-closed discipline (ADR-0007 §2): a budget limit must be a
    real number ``≥ 0`` — a negative, ``NaN``, ``Infinity``, or unparseable value is a
    ``configuration_error`` so admission's ``(limit − spent) ≥ estimate`` never compares
    against a non-number or a negative ceiling.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for any non-finite or negative value.
    """
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
    """Audit an admin set-op under the nil UUID, carrying the project + values in args."""
    await audit.record(
        conn,
        ctx,
        tool=f"accounting.{tool}",
        object_kind="budgets" if tool == "set_budget" else "quotas",
        object_id=_ACCOUNTING_AUDIT_ID,
        transition=f"{tool}:applied",
        args={"project": project, **values},
        project=project,
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `accounting.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="accounting.estimate")
    async def accounting_estimate(
        project: str,
        vcpus: int,
        memory_gb: int,
        window: float | str,
        cost_class: str = _DEFAULT_COST_CLASS,
    ) -> ToolResponse:
        # `window` accepts a number or a decimal string so a precise caller can pass an
        # exact window; `parse_window_hours` does one `Decimal(str(window))` conversion
        # shared with admission, and a non-numeric value fails closed (configuration_error).
        return await estimate(
            pool,
            current_context(),
            project=project,
            vcpus=vcpus,
            memory_gb=memory_gb,
            window=window,
            cost_class=cost_class,
        )

    @app.tool(name="accounting.usage")
    async def accounting_usage(
        project: str | None = None, investigation_id: str | None = None
    ) -> ToolResponse:
        # Exactly one of project / investigation_id; the investigation form resolves the
        # owning project and authorizes on it (no cross-project read bypass, ADR-0007 §6).
        return await usage(
            pool,
            current_context(),
            project=project,
            investigation_id=investigation_id,
        )

    @app.tool(name="accounting.report")
    async def accounting_report(
        scope: str,
        projects: list[str] | None = None,
        group_by: str | None = None,
        window: list[str | None] | None = None,
    ) -> ToolResponse:
        # Two scope forms (ADR-0043 §3): `granted-set` rides per-project require_role(viewer)
        # over the caller's members; `all-projects` is gated platform_auditor. Read-audited
        # to platform_audit_log by read-shape (granted-set) or always (all-projects).
        return await report(
            pool,
            current_context(),
            scope=scope,
            projects=projects,
            group_by=group_by,
            window=window,
        )

    @app.tool(name="accounting.set_budget")
    async def accounting_set_budget(project: str, limit_kcu: float | str) -> ToolResponse:
        # admin-only; `limit_kcu` accepts a number or a decimal string (precise limit).
        return await set_budget(pool, current_context(), project=project, limit_kcu=limit_kcu)

    @app.tool(name="accounting.set_quota")
    async def accounting_set_quota(
        project: str, max_concurrent_allocations: int, max_concurrent_systems: int
    ) -> ToolResponse:
        # admin-only; both caps must be >= 0 (a negative cap is a configuration_error).
        return await set_quota(
            pool,
            current_context(),
            project=project,
            max_concurrent_allocations=max_concurrent_allocations,
            max_concurrent_systems=max_concurrent_systems,
        )
