"""Shared auth/audit helpers for ``images.*`` operator tools."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

UPLOAD_TOOL = "images.upload"
DELETE_TOOL = "images.delete"
PRUNE_TOOL = "images.prune_expired"
EXTEND_TOOL = "images.extend"
OBJECT_KIND = "image_catalog"
PRUNE_OBJECT_ID = "expired-private"
PRUNE_SCOPE = "all-private"


def denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def blank(reason: str) -> bool:
    return not reason.strip()


async def audit_project_denial(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    project: str,
    args: dict[str, object],
) -> None:
    """Record a project-role denial row on its own connection before any pool mutation."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_denial(
            conn,
            event=audit.DenialEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                tool=tool,
                args=args,
                reason=f"{ctx.principal!r} may not {tool} in project {project!r}",
            ),
        )


async def record_admin_breakglass(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    scope: str,
    args: dict[str, object],
) -> None:
    """Write the always-on ``platform_audit_log`` accountability row in its own transaction."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=scope,
                args=args,
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


__all__ = [
    "DELETE_TOOL",
    "EXTEND_TOOL",
    "OBJECT_KIND",
    "PRUNE_OBJECT_ID",
    "PRUNE_SCOPE",
    "PRUNE_TOOL",
    "UPLOAD_TOOL",
    "PlatformRole",
    "audit_platform_denial",
    "blank",
    "denied",
    "record_admin_breakglass",
    "audit_project_denial",
    "held_platform_roles",
    "actor_for",
]
