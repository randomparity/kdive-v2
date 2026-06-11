"""``ops.diagnostics`` — the authz-gated aggregating diagnostics tool (ADR-0091 §1,2,4).

The server-side `doctor` head: it runs each read-only check from its correct vantage and
returns one coherent verdict, so an operator never has to probe the worker→hypervisor or
secret-backend hops from a laptop they cannot see them from. Gated to ``platform_operator``
(a ``platform_admin``-alone token is denied — admin implies only auditor) and audited under
the resolved ``(principal, operator-cli)`` actor on both the served run and the over-reach
denial (ADR-0006, ADR-0089).

The verdict keeps the three-state distinction: each item carries the check's
``status``/``detail``/``fix``/``provider``, and a down dependency surfaces as an ``error``
item (reported distinctly) rather than a contract ``fail`` — the tool that explains
breakage must not emit a confident wrong fix when a backend was simply down.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.diagnostics.checks import CheckResult
from kdive.diagnostics.service import DiagnosticsService
from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops._auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.security import audit
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_TOOL = "ops.diagnostics"
_OBJECT_ID = "diagnostics"

# Builds the service for a provider target (a named provider, or None for all registered).
ServiceFactory = Callable[[str | None], DiagnosticsService]


def _denied() -> ToolResponse:
    return ToolResponse.failure(
        _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
    )


async def run_diagnostics(
    pool: AsyncConnectionPool,
    service_factory: ServiceFactory,
    ctx: RequestContext,
    *,
    provider: str | None = None,
) -> ToolResponse:
    """Run the read-only diagnostics and return one coherent verdict; operator-gated.

    A caller without ``platform_operator`` is denied; the denial is audited iff the caller
    holds any platform role (the over-reach accountability row), and the served run is
    always audited under the resolved actor. The verdict carries each check's three-state
    result; an ``error`` item is reported distinctly and never inflated into a ``fail``.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool, ctx, tool=_TOOL, scope=ALL_PROJECTS_SCOPE, args=_audit_args(provider)
            )
            return _denied()
        report = await service_factory(provider).run()
        await _audit_run(pool, ctx, provider)
        return _verdict(report.results, report.has_failure, report.has_error)


def _audit_args(provider: str | None) -> dict[str, object]:
    return {"provider": provider}


async def _audit_run(pool: AsyncConnectionPool, ctx: RequestContext, provider: str | None) -> None:
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_audit_args(provider),
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def _item(result: CheckResult) -> ToolResponse:
    return ToolResponse.success(
        result.check_id,
        "ok",
        data={
            "check": result.check_id,
            "status": result.status.value,
            "detail": result.detail,
            "fix": result.fix,
            "provider": result.provider,
        },
    )


def _verdict(results: list[CheckResult], has_failure: bool, has_error: bool) -> ToolResponse:
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        [_item(r) for r in results],
        suggested_next_actions=[_TOOL],
        data={
            "has_failure": "true" if has_failure else "false",
            "has_error": "true" if has_error else "false",
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool, service_factory: ServiceFactory) -> None:
    """Register ``ops.diagnostics`` on ``app``, bound to ``pool`` and the ``service_factory``."""

    @app.tool(name=_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def ops_diagnostics(
        provider: Annotated[
            str | None,
            Field(description="Diagnose one named registered provider; omit for all registered."),
        ] = None,
    ) -> ToolResponse:
        """Run the read-only deployment diagnostics. Platform operator-gated.

        Returns one verdict carrying each check's three-state status, detail, fix, and the
        provider it covered. A check that could not be run (a down dependency) reports an
        ``error`` distinctly — it is not a contract failure.
        """
        return await run_diagnostics(pool, service_factory, current_context(), provider=provider)
