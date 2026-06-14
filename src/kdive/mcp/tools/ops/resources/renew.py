"""``resources.renew`` — extend a runtime resource's lease (M2.6 #396, ADR-0112).

The lease is keyed to the ``resource_id``, **not** to the registering session: any
``platform_admin`` (or a successor agent after a handoff) can renew, so a long-lived runtime
resource survives the loss of the agent that registered it. Renewal extends
``lease_expires_at`` to ``now() + KDIVE_RESOURCE_LEASE_TTL_SECONDS`` (the same window
``register`` applies), which is what the reconciler's lease reaper reads.

Operates only on ``managed_by='runtime'`` rows — config/discovery rows carry no lease.

Authorization: ``platform_admin``. Audit: one ``platform_audit_log`` row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import RESOURCE_LEASE_TTL_SECONDS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ManagedBy
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.tools.ops.resources._common import RENEW_TOOL, config_error, denied
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)


async def renew_resource(
    pool: AsyncConnectionPool, ctx: RequestContext, *, resource_id: str
) -> ToolResponse:
    """Extend a runtime resource's lease. Requires ``platform_admin``.

    Keyed to ``resource_id`` (survives agent handoff). Sets ``lease_expires_at`` to
    ``now() + KDIVE_RESOURCE_LEASE_TTL_SECONDS`` on the ``managed_by='runtime'`` row.

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        resource_id: The Resource UUID whose lease to extend.

    Returns:
        A success envelope carrying the new ``lease_expires_at``, or a typed failure envelope
        (authorization_denied / not_found / configuration_error / conflict).
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=RENEW_TOOL,
            scope=f"denied:{resource_id}",
            args={"resource_id": resource_id},
        )
        return denied(resource_id, RENEW_TOOL)

    uid = _as_uuid(resource_id)
    if uid is None:
        return config_error(resource_id, "resource_id is not a valid UUID")

    new_deadline = datetime.now(UTC) + timedelta(seconds=config.require(RESOURCE_LEASE_TTL_SECONDS))
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.transaction():
            updated = await _extend_lease(conn, uid, new_deadline)
            if updated is None:
                return await _classify_unrenewable(conn, uid, resource_id)
            await _audit_renew(conn, ctx, resource_id=uid, deadline=new_deadline)

    _log.info("runtime resource %s lease renewed by %s", uid, ctx.principal)
    return ToolResponse.success(
        resource_id,
        "renewed",
        suggested_next_actions=["resources.list"],
        data={"id": resource_id, "lease_expires_at": new_deadline.isoformat()},
    )


async def _extend_lease(
    conn: AsyncConnection, uid: UUID, deadline: datetime
) -> dict[str, object] | None:
    """Set ``lease_expires_at`` on the runtime row; return it, or ``None`` if no runtime row."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE resources SET lease_expires_at = %s WHERE id = %s AND managed_by = %s "
            "RETURNING id",
            (deadline, uid, ManagedBy.RUNTIME.value),
        )
        return await cur.fetchone()


async def _classify_unrenewable(conn: AsyncConnection, uid: UUID, resource_id: str) -> ToolResponse:
    """Distinguish an absent id (not_found) from a non-runtime row (conflict — no lease)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT managed_by FROM resources WHERE id = %s", (uid,))
        row = await cur.fetchone()
    if row is None:
        return ToolResponse.failure(resource_id, ErrorCategory.NOT_FOUND)
    return ToolResponse.failure(
        resource_id,
        ErrorCategory.CONFLICT,
        data={
            "reason": (
                f"resource is managed_by={row['managed_by']!r}; only runtime-registered "
                "resources carry a renewable lease"
            )
        },
    )


async def _audit_renew(
    conn: AsyncConnection, ctx: RequestContext, *, resource_id: UUID, deadline: datetime
) -> None:
    """Write the renew audit row."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=RENEW_TOOL,
            scope=f"resource:{resource_id}",
            args={"resource_id": str(resource_id), "lease_expires_at": deadline.isoformat()},
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )


__all__ = ["renew_resource"]
