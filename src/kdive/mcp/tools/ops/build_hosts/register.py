"""``build_hosts.register_*`` handlers — register new remote build hosts.

Two remote kinds are registerable: ``ssh`` and ``ephemeral_libvirt``. The
``worker-local`` ``local`` seed is injected at migration time and is not reproduced
through this path.

Authorization: ``platform_admin`` only.
Audit: one ``platform_audit_log`` row (never containing secret bytes).
"""

from __future__ import annotations

import logging
from typing import LiteralString

import psycopg.errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import BuildHostKind
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

REGISTER_SSH_TOOL = "build_hosts.register_ssh"
REGISTER_EPHEMERAL_LIBVIRT_TOOL = "build_hosts.register_ephemeral_libvirt"


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def _config_error(name: str, reason: str) -> ToolResponse:
    return ToolResponse.failure(name, ErrorCategory.CONFIGURATION_ERROR, data={"reason": reason})


def _validate_credential_ref(ref: str | None) -> bool:
    """Return True iff ``ref`` is a non-empty, non-blank credential reference string.

    We validate presence and non-blankness only — the bytes are never fetched here,
    keeping this tool free of secret material.
    """
    return bool(ref and ref.strip())


# A validated INSERT plan: a literal statement (fixed column set per kind, so the SQL stays a
# LiteralString — no dynamic SQL) plus its bound values; or a typed failure envelope.
_SSH_INSERT: LiteralString = (
    "INSERT INTO build_hosts "
    "  (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"
)
_EPHEMERAL_INSERT: LiteralString = (
    "INSERT INTO build_hosts "
    "  (name, kind, base_image_volume, workspace_root, max_concurrent) "
    "VALUES (%s, %s, %s, %s, %s) RETURNING id"
)


def _ssh_plan(
    name: str,
    address: str | None,
    ssh_credential_ref: str | None,
    base_image_volume: str | None,
    workspace_root: str,
    max_concurrent: int,
) -> tuple[LiteralString, tuple[object, ...]] | ToolResponse:
    if not _validate_credential_ref(ssh_credential_ref):
        return _config_error(name, "ssh_credential_ref must be a non-blank reference string")
    if not (address and address.strip()):
        return _config_error(name, "an ssh build host requires an address")
    if base_image_volume:
        return _config_error(name, "base_image_volume is not valid for an ssh build host")
    return _SSH_INSERT, (
        name,
        BuildHostKind.SSH.value,
        address,
        ssh_credential_ref,
        workspace_root,
        max_concurrent,
    )


def _ephemeral_plan(
    name: str,
    address: str | None,
    ssh_credential_ref: str | None,
    base_image_volume: str | None,
    workspace_root: str,
    max_concurrent: int,
) -> tuple[LiteralString, tuple[object, ...]] | ToolResponse:
    if not (base_image_volume and base_image_volume.strip()):
        return _config_error(name, "an ephemeral_libvirt build host requires a base_image_volume")
    if address or ssh_credential_ref:
        return _config_error(
            name, "address/ssh_credential_ref are not valid for an ephemeral_libvirt build host"
        )
    return _EPHEMERAL_INSERT, (
        name,
        BuildHostKind.EPHEMERAL_LIBVIRT.value,
        base_image_volume,
        workspace_root,
        max_concurrent,
    )


async def _register_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    kind: BuildHostKind,
    name: str,
    workspace_root: str,
    max_concurrent: int,
    address: str | None = None,
    ssh_credential_ref: str | None = None,
    base_image_volume: str | None = None,
) -> ToolResponse:
    """INSERT a new remote build host row. Requires ``platform_admin``.

    Two remote kinds are registerable (the ``local`` ``worker-local`` seed is injected at
    migration time, not through this path):

    - ``ssh`` — requires ``address`` + ``ssh_credential_ref``; ``base_image_volume``
      must be absent.
    - ``ephemeral_libvirt`` — requires ``base_image_volume``; ``address``/``ssh_credential_ref``
      must be absent (the build VM lives on the configured remote-libvirt host; it has no SSH
      credential).

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        name: Unique human-readable identifier for the new host.
        workspace_root: Absolute path where builds are staged (in-guest for ephemeral).
        max_concurrent: Maximum simultaneous build leases (must be > 0).
        kind: ``'ssh'`` or ``'ephemeral_libvirt'``.
        address: SSH hostname or IP (ssh only).
        ssh_credential_ref: Credential secret reference (ssh only) — only the reference string
            is stored and returned; secret bytes are never fetched or logged.
        base_image_volume: Operator-staged base build-image volume (ephemeral_libvirt only).

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
            tool=tool,
            scope=f"denied:{name}",
            args={"name": name},
        )
        return _denied(name, tool)

    if max_concurrent <= 0:
        return _config_error(name, "max_concurrent must be a positive integer")

    if kind is BuildHostKind.SSH:
        plan = _ssh_plan(
            name, address, ssh_credential_ref, base_image_volume, workspace_root, max_concurrent
        )
    elif kind is BuildHostKind.EPHEMERAL_LIBVIRT:
        plan = _ephemeral_plan(
            name, address, ssh_credential_ref, base_image_volume, workspace_root, max_concurrent
        )
    else:
        return _config_error(name, f"unsupported build host kind {kind.value!r}")
    if isinstance(plan, ToolResponse):
        return plan
    insert_sql, values = plan

    try:
        async with pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(insert_sql, values)
                row = await cur.fetchone()
            assert row is not None
            host_id = row["id"]

            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=tool,
                    scope=f"build_host:{host_id}",
                    args={
                        "name": name,
                        "kind": kind.value,
                        "address": address,
                        "ssh_credential_ref": ssh_credential_ref,
                        "base_image_volume": base_image_volume,
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


async def register_ssh_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    address: str,
    ssh_credential_ref: str,
    workspace_root: str,
    max_concurrent: int,
) -> ToolResponse:
    """INSERT a new SSH build host row. Requires ``platform_admin``."""
    return await _register_build_host(
        pool,
        ctx,
        tool=REGISTER_SSH_TOOL,
        kind=BuildHostKind.SSH,
        name=name,
        address=address,
        ssh_credential_ref=ssh_credential_ref,
        workspace_root=workspace_root,
        max_concurrent=max_concurrent,
    )


async def register_ephemeral_libvirt_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    base_image_volume: str,
    workspace_root: str,
    max_concurrent: int,
) -> ToolResponse:
    """INSERT a new ephemeral-libvirt build host row. Requires ``platform_admin``."""
    return await _register_build_host(
        pool,
        ctx,
        tool=REGISTER_EPHEMERAL_LIBVIRT_TOOL,
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
        name=name,
        base_image_volume=base_image_volume,
        workspace_root=workspace_root,
        max_concurrent=max_concurrent,
    )


__all__ = [
    "REGISTER_EPHEMERAL_LIBVIRT_TOOL",
    "REGISTER_SSH_TOOL",
    "register_ephemeral_libvirt_build_host",
    "register_ssh_build_host",
]
