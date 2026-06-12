"""``secrets.list`` — secret *presence* (references), platform-operator gated (ADR-0089 §6).

Off the general agent surface: secret presence is a reconnaissance primitive, so this gates
on ``platform_operator`` (a token holding ``platform_admin`` alone is denied — admin implies
only auditor). It returns the *references* (scope keys) secrets are registered under — never
their values; :meth:`SecretRegistry.scope_refs` is a value-free projection. Like every
cross-platform ``ops.*`` read it audits both the served read and the over-reach denial (when
the caller holds any platform role), recording the resolved ``actor``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.security import audit
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.security.secrets.secret_registry import SecretRegistry

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_TOOL = "secrets.list"
_OBJECT_ID = "secrets"


def _denied() -> ToolResponse:
    return ToolResponse.failure(
        _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
    )


async def list_secrets_tool(
    pool: AsyncConnectionPool, registry: SecretRegistry, ctx: RequestContext
) -> ToolResponse:
    """List registered secret *references* (presence); platform_operator-gated, audited.

    Returns the scope keys secrets are registered under (never the values). A caller without
    ``platform_operator`` is denied; the denial is audited iff the caller holds any platform
    role (the over-reach accountability row), and the served read is always audited.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
    except AuthorizationError:
        await audit_platform_denial(pool, ctx, tool=_TOOL, scope=ALL_PROJECTS_SCOPE)
        return _denied()
    refs = sorted(registry.scope_refs())
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args={},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )
    return ToolResponse.success(_OBJECT_ID, "ok", data={"secrets": refs})


def register(app: FastMCP, pool: AsyncConnectionPool, registry: SecretRegistry) -> None:
    """Register ``secrets.list`` on ``app``, bound to ``pool`` and the secret ``registry``."""

    @app.tool(name=_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def secrets_list() -> ToolResponse:
        """List secret *references* (presence only — never values). Platform operator-gated."""
        return await list_secrets_tool(pool, registry, current_context())
