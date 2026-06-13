"""``build_hosts.register`` handler — register a new SSH build host.

Only SSH hosts are registerable via this tool. The ``worker-local`` seed is
injected at migration time and is not reproduced through this path.

Authorization: ``platform_admin`` only.
Audit: one ``platform_audit_log`` row (never containing secret bytes).
"""

from __future__ import annotations

import logging

import psycopg.errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

REGISTER_TOOL = "build_hosts.register"


def _denied(object_id: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[REGISTER_TOOL]
    )


def _validate_credential_ref(ref: str) -> bool:
    """Return True iff ``ref`` is a non-empty, non-blank credential reference string.

    We validate presence and non-blankness only — the bytes are never fetched here,
    keeping this tool free of secret material.
    """
    return bool(ref and ref.strip())


async def register_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    address: str,
    ssh_credential_ref: str,
    workspace_root: str,
    max_concurrent: int,
) -> ToolResponse:
    """INSERT a new SSH build host row. Requires ``platform_admin``.

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        name: Unique human-readable identifier for the new host.
        address: SSH hostname or IP address (required for SSH hosts).
        ssh_credential_ref: Credential secret reference — only the reference string is
            stored and returned; secret bytes are never fetched or logged.
        workspace_root: Absolute path on the build host where builds are staged.
        max_concurrent: Maximum simultaneous build leases (must be > 0).

    Returns:
        A success envelope with the new host id and suggested next actions, or a
        typed failure envelope (authorization_denied / conflict / configuration_error).
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=REGISTER_TOOL,
            scope=f"denied:{name}",
            args={"name": name},
        )
        return _denied(name)

    if not _validate_credential_ref(ssh_credential_ref):
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFIGURATION_ERROR,
            data={"reason": "ssh_credential_ref must be a non-blank reference string"},
        )

    if max_concurrent <= 0:
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFIGURATION_ERROR,
            data={"reason": "max_concurrent must be a positive integer"},
        )

    try:
        async with pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "INSERT INTO build_hosts "
                    "  (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent) "
                    "VALUES (%s, 'ssh', %s, %s, %s, %s) "
                    "RETURNING id",
                    (name, address, ssh_credential_ref, workspace_root, max_concurrent),
                )
                row = await cur.fetchone()
            assert row is not None
            host_id = row["id"]

            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=REGISTER_TOOL,
                    scope=f"build_host:{host_id}",
                    args={
                        "name": name,
                        "address": address,
                        "ssh_credential_ref": ssh_credential_ref,
                        "workspace_root": workspace_root,
                        "max_concurrent": max_concurrent,
                    },
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
    except psycopg.errors.UniqueViolation:
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFLICT,
            data={"reason": f"a build host named {name!r} already exists"},
        )

    _log.info("build host %r (%s) registered by %s", name, host_id, ctx.principal)
    return ToolResponse.success(
        str(host_id),
        "registered",
        suggested_next_actions=["build_hosts.list", "runs.build"],
        data={"id": str(host_id), "name": name},
    )
