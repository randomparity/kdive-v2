"""Shared platform authorization audit helpers for operator tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.security import audit
from kdive.security.authz.actor import resolve_actor

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

ALL_PROJECTS_SCOPE = "all-projects"


def held_platform_roles(ctx: RequestContext) -> str | None:
    """Return the caller's platform roles as a sorted comma string, or None if absent."""
    if not ctx.platform_roles:
        return None
    return ",".join(sorted(role.value for role in ctx.platform_roles))


def actor_for(ctx: RequestContext) -> str:
    """Resolve the audit ``actor`` (operator-cli | agent | unknown) for ``ctx`` (ADR-0089)."""
    return resolve_actor(
        ctx.client_id,
        agent_session=ctx.agent_session,
        cli_client_id=config.require(CLI_CLIENT_ID),
    )


async def audit_platform_denial(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    scope: str,
    args: dict[str, object] | None = None,
) -> None:
    """Audit a platform-role denial iff the caller holds any platform role.

    Project-only denials are the routine non-grant case and are not recorded. Platform
    role overreach is recorded so separation-of-duties denials leave an accountability row
    without letting ordinary authenticated tokens amplify writes.
    """
    held = held_platform_roles(ctx)
    if held is None:
        return
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=scope,
                args={} if args is None else args,
                platform_role=held,
                actor=actor_for(ctx),
            ),
        )
