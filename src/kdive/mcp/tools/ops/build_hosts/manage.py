"""``build_hosts.list``, ``build_hosts.disable``, and ``build_hosts.remove`` handlers.

``list`` is read-only-by-policy: returns id, name, kind, address, ssh_credential_ref
(the reference string only — never key bytes), workspace_root, max_concurrent, enabled,
and state for every row in ``build_hosts``.

``disable`` and ``remove`` are ``platform_admin``-gated mutating ops. Both reject the
protected ``worker-local`` seed (CONFLICT). ``remove`` also rejects a host that still
holds active leases (FK ON DELETE RESTRICT, surfaced as CONFLICT).
"""

from __future__ import annotations

import logging

import psycopg.errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import WORKER_LOCAL_ID, get_by_name
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

LIST_TOOL = "build_hosts.list"
DISABLE_TOOL = "build_hosts.disable"
REMOVE_TOOL = "build_hosts.remove"

_OBJECT_KIND = "build_host"
_ALL_HOSTS_SCOPE = "all-build-hosts"
_PROTECTED_NAME = "worker-local"


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def _conflict(object_id: str, reason: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFLICT, data={"reason": reason})


def _not_found(name: str) -> ToolResponse:
    return ToolResponse.failure(name, ErrorCategory.NOT_FOUND)


async def _record_platform_action(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    scope: str,
    args: dict[str, object],
) -> None:
    """Write a ``platform_audit_log`` row in its own committed transaction."""
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


async def list_build_hosts(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
) -> ToolResponse:
    """Return all build host rows (read-only, no auth gate required).

    The response includes only the ``ssh_credential_ref`` reference string — never
    key bytes.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, name, kind, address, ssh_credential_ref, workspace_root, "
            "       max_concurrent, enabled, state "
            "FROM build_hosts ORDER BY name"
        )
        rows = await cur.fetchall()

    items = [
        ToolResponse.success(
            str(row["id"]),
            "ok",
            data={
                "id": str(row["id"]),
                "name": row["name"],
                "kind": row["kind"],
                "address": row["address"] or "",
                "ssh_credential_ref": row["ssh_credential_ref"] or "",
                "workspace_root": row["workspace_root"],
                "max_concurrent": str(row["max_concurrent"]),
                "enabled": str(row["enabled"]).lower(),
                "state": row["state"],
            },
        )
        for row in rows
    ]
    return ToolResponse.collection("build_hosts", "ok", items)


async def disable_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
) -> ToolResponse:
    """Set ``enabled=false`` on the named host. Requires ``platform_admin``.

    Rejects the ``worker-local`` seed (CONFLICT) and an absent name (NOT_FOUND).
    Writes a ``platform_audit_log`` row on success.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=DISABLE_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return _denied(name, DISABLE_TOOL)

    if name == _PROTECTED_NAME:
        return _conflict(name, f"{name!r} is a protected fallback and cannot be disabled")

    async with pool.connection() as conn:
        host = await get_by_name(conn, name)
        if host is None:
            return _not_found(name)
        if host.id == WORKER_LOCAL_ID:
            return _conflict(name, f"{name!r} is a protected fallback and cannot be disabled")
        async with conn.transaction():
            await conn.execute("UPDATE build_hosts SET enabled = false WHERE id = %s", (host.id,))
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=DISABLE_TOOL,
                    scope=f"build_host:{host.id}",
                    args={"name": name, "host_id": str(host.id)},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )

    _log.info("build host %r (%s) disabled by %s", name, host.id, ctx.principal)
    return ToolResponse.success(
        str(host.id),
        "disabled",
        suggested_next_actions=[LIST_TOOL],
        data={"name": name},
    )


async def remove_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
) -> ToolResponse:
    """Delete the named host row. Requires ``platform_admin``.

    Rejects the ``worker-local`` seed (CONFLICT), a host with outstanding leases
    (CONFLICT — FK ON DELETE RESTRICT), and an absent name (NOT_FOUND). Writes a
    ``platform_audit_log`` row on success.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=REMOVE_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return _denied(name, REMOVE_TOOL)

    if name == _PROTECTED_NAME:
        return _conflict(name, f"{name!r} is a protected fallback and cannot be removed")

    async with pool.connection() as conn:
        host = await get_by_name(conn, name)
        if host is None:
            return _not_found(name)
        if host.id == WORKER_LOCAL_ID:
            return _conflict(name, f"{name!r} is a protected fallback and cannot be removed")

        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM build_hosts WHERE id = %s", (host.id,))
                await audit.record_platform(
                    conn,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    event=audit.PlatformAuditEvent(
                        tool=REMOVE_TOOL,
                        scope=f"build_host:{host.id}",
                        args={"name": name, "host_id": str(host.id)},
                        platform_role=held_platform_roles(ctx),
                        actor=actor_for(ctx),
                    ),
                )
        except psycopg.errors.ForeignKeyViolation:
            return _conflict(
                name,
                f"build host {name!r} has active leases and cannot be removed",
            )

    _log.info("build host %r (%s) removed by %s", name, host.id, ctx.principal)
    return ToolResponse.success(
        str(host.id),
        "removed",
        suggested_next_actions=[LIST_TOOL],
        data={"name": name},
    )
