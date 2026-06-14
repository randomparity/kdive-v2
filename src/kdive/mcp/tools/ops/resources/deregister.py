"""``resources.deregister`` — remove a runtime provider resource (M2.6 #396, ADR-0112).

Operates **only** on ``managed_by='runtime'`` rows: a ``config``-owned (or ``discovery``-owned)
instance is rejected (a config resource is removed by editing ``systems.toml``). Deregistering a
resource that still carries **live** allocations is destructive-tier — like
``ops.force_teardown`` it requires ``platform_admin`` **plus** explicit ``force=True``
confirmation; without it the call is refused so a live debugging session is never silently
evicted.

Disposition is FK-safe: allocation rows (live or terminal) are retained for accounting and keep
an unconditional FK to ``resources``, so a resource that ever held an allocation is **cordoned**
(stops new placement) and cleared of its lease rather than row-deleted — the same non-destructive
disposition the config-prune and lease-reaper contracts use (ADR-0112). A never-allocated
resource is hard-deleted. The success envelope's ``disposition`` reports ``deleted`` or
``cordoned``.

Authorization: ``platform_admin``. Audit: one ``platform_audit_log`` row.
"""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ManagedBy
from kdive.domain.state import AllocationState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.tools.ops.resources._common import DEREGISTER_TOOL, config_error, denied
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

# Allocation states that hold a slot on the host (a deregister of a host carrying these is
# destructive). `requested` is queued, not holding the host, so it does not gate deregister.
_LIVE = (AllocationState.GRANTED, AllocationState.ACTIVE, AllocationState.RELEASING)


async def deregister_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resource_id: str,
    force: bool = False,
) -> ToolResponse:
    """Delete a runtime resource row. Requires ``platform_admin`` (``force`` if live).

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        resource_id: The Resource UUID to deregister.
        force: Typed confirmation required when the resource carries live allocations.

    Returns:
        A success envelope, or a typed failure envelope (authorization_denied / not_found /
        configuration_error / conflict).
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=DEREGISTER_TOOL,
            scope=f"denied:{resource_id}",
            args={"resource_id": resource_id, "force": force},
        )
        return denied(resource_id, DEREGISTER_TOOL)

    uid = _as_uuid(resource_id)
    if uid is None:
        return config_error(resource_id, "resource_id is not a valid UUID")

    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.transaction():
            row = await _locked_runtime_row(conn, uid)
            if row is None:
                return await _classify_absent(conn, uid, resource_id)
            live = await _live_count(conn, uid)
            if live and not force:
                return ToolResponse.failure(
                    resource_id,
                    ErrorCategory.CONFLICT,
                    data={
                        "reason": f"{live} live allocation(s); pass force=true to deregister",
                        "live_allocations": str(live),
                    },
                    suggested_next_actions=["resources.drain", DEREGISTER_TOOL],
                )
            disposition = await _remove(conn, uid)
            await _audit_deregister(
                conn, ctx, resource_id=uid, force=force, live=live, disposition=disposition
            )

    _log.info(
        "runtime resource %s deregistered (%s) by %s (force=%s)",
        uid,
        disposition,
        ctx.principal,
        force,
    )
    return ToolResponse.success(
        resource_id,
        "deregistered",
        suggested_next_actions=["resources.list"],
        data={
            "id": resource_id,
            "forced": "true" if force else "false",
            "disposition": disposition,
        },
    )


async def _locked_runtime_row(conn: AsyncConnection, uid: UUID) -> dict[str, object] | None:
    """SELECT … FOR UPDATE the resource row only if it is ``managed_by='runtime'``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, managed_by FROM resources WHERE id = %s AND managed_by = %s FOR UPDATE",
            (uid, ManagedBy.RUNTIME.value),
        )
        return await cur.fetchone()


async def _classify_absent(conn: AsyncConnection, uid: UUID, resource_id: str) -> ToolResponse:
    """Distinguish a truly-absent id (not_found) from a config/discovery row (conflict)."""
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
                "resources are deregistered here (a config resource is removed by editing "
                "systems.toml)"
            )
        },
    )


async def _live_count(conn: AsyncConnection, uid: UUID) -> int:
    """Count allocations holding a slot on the resource."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state = ANY(%s)",
            (uid, [s.value for s in _LIVE]),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _remove(conn: AsyncConnection, uid: UUID) -> str:
    """Hard-delete an allocation-free row, else cordon (FK-safe soft-deregister).

    Allocation rows (live **or** terminal) are retained for accounting/audit and keep an
    unconditional FK to ``resources``, so a resource that ever held an allocation cannot be
    row-deleted. Such a resource is instead **cordoned** (stops new placement) and cleared of
    its lease so the reconciler stops renewing/reaping it — the same non-destructive disposition
    the config-prune and lease-reaper contracts use (ADR-0112). A never-allocated runtime row is
    hard-deleted. Returns ``'deleted'`` or ``'cordoned'``.
    """
    if await _has_any_allocation(conn, uid):
        await conn.execute(
            "UPDATE resources SET cordoned = true, lease_expires_at = NULL "
            "WHERE id = %s AND managed_by = %s",
            (uid, ManagedBy.RUNTIME.value),
        )
        return "cordoned"
    await conn.execute(
        "DELETE FROM resources WHERE id = %s AND managed_by = %s",
        (uid, ManagedBy.RUNTIME.value),
    )
    return "deleted"


async def _has_any_allocation(conn: AsyncConnection, uid: UUID) -> bool:
    """Whether any allocation row (any state) FK-references the resource."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM allocations WHERE resource_id = %s LIMIT 1", (uid,))
        return (await cur.fetchone()) is not None


async def _audit_deregister(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    resource_id: UUID,
    force: bool,
    live: int,
    disposition: str,
) -> None:
    """Write the deregister audit row."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=DEREGISTER_TOOL,
            scope=f"resource:{resource_id}:{disposition}",
            args={
                "resource_id": str(resource_id),
                "force": force,
                "live_allocations": live,
                "disposition": disposition,
            },
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )


__all__ = ["deregister_resource"]
