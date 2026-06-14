"""``resources.register`` — register a runtime provider resource (M2.6 #396, ADR-0112).

Imperative agent-native capacity registration. Writes a ``managed_by='runtime'`` row keyed by
``(kind, name)`` so it never collides with a declarative ``config`` row (those are removed by
editing ``systems.toml``, not by this tool). A ``name`` already owned by a ``config`` row is
rejected — the file owns that identity.

Authorization: ``platform_admin`` only. Audit: one ``platform_audit_log`` row (never carrying
secret bytes — only the secret *reference* strings are recorded).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg.errors
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import RESOURCE_LEASE_TTL_SECONDS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ManagedBy, ResourceKind, ResourceStatus
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.inventory.reconcile import resource_identity_lock
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.tools.ops.resources._common import (
    REGISTER_TOOL,
    ResourceProbe,
    TcpResourceProbe,
    config_error,
    denied,
    resolve_block_kind,
    secret_ref_resolves,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.security.secrets.secrets import secrets_root_from_env

_log = logging.getLogger(__name__)

_FAULT_INJECT_HOST_URI = "fault-inject://local"


def _lease_deadline() -> datetime:
    """``now() + KDIVE_RESOURCE_LEASE_TTL_SECONDS`` — the runtime-resource lease horizon."""
    return datetime.now(UTC) + timedelta(seconds=config.require(RESOURCE_LEASE_TTL_SECONDS))


def _resolve_owner_project(
    ctx: RequestContext, owner_project: str | None
) -> str | None | ToolResponse:
    """Resolve the owner project: explicit wins; else default to the single registering project.

    A global (``None``) resource is requested with the literal sentinel ``"*"``. When no explicit
    project is given and the caller holds exactly one project, that project is the default; an
    ambiguous (multiple) or absent project membership requires an explicit ``owner_project``.
    """
    if owner_project == "*":
        return None
    if owner_project is not None:
        return owner_project
    if len(ctx.projects) == 1:
        return ctx.projects[0]
    return config_error(
        "owner_project",
        "owner_project could not be defaulted: caller has no single registering project; "
        "pass owner_project explicitly (or '*' for a global resource)",
    )


async def _network_preflight(
    *,
    kind: ResourceKind,
    name: str,
    host_uri: str,
    secret_refs: tuple[str, ...],
    base_image: str | None,
    probe: ResourceProbe,
    secrets_root: Path,
) -> ToolResponse | None:
    """Run the non-DB preflight (secret refs + reachability); fail envelope, or ``None``.

    Deliberately runs **before** any DB connection/transaction is opened so the bounded TCP
    probe and the filesystem secret-ref reads never stall a pooled connection (or hold a row
    lock) for the network round-trip.

    * ``remote-libvirt`` — every cert/secret ref resolves + reachability probe + ``base_image``
      is present (its ``registered`` status is checked under the DB transaction).
    * ``local-libvirt`` — host reachability only (no ``base_image``).
    * ``fault-inject`` — secret ref resolves only (synthetic host; **no** reachability, **no**
      ``base_image`` — a missing ``base_image`` never fails a fault-inject register).
    """
    for ref in secret_refs:
        if not secret_ref_resolves(ref, secrets_root):
            return config_error(name, f"secret reference {ref!r} does not resolve")
    if kind is ResourceKind.FAULT_INJECT:
        return None
    if not await probe.probe(host_uri):
        return config_error(name, f"host {host_uri!r} is not reachable")
    if kind is ResourceKind.REMOTE_LIBVIRT and not base_image:
        return config_error(name, "remote-libvirt requires a base_image")
    return None


async def _db_preflight(
    conn: AsyncConnection, *, kind: ResourceKind, name: str, base_image: str | None
) -> ToolResponse | None:
    """Run the DB-dependent preflight (config-name collision + base_image registered).

    Held inside the write transaction so the collision check and the INSERT are atomic.
    """
    if await _reject_config_name(conn, kind, name):
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFLICT,
            data={"reason": f"{name!r} is a config-managed resource; edit systems.toml"},
        )
    if kind is ResourceKind.REMOTE_LIBVIRT:
        assert base_image is not None  # _network_preflight rejected a missing base_image
        if not await _base_image_registered(conn, kind, base_image):
            return config_error(
                name, f"base_image {base_image!r} is not a registered image for {kind.value}"
            )
    return None


async def _base_image_registered(
    conn: AsyncConnection, kind: ResourceKind, base_image: str
) -> bool:
    """Whether ``base_image`` names a ``registered`` image_catalog row for ``kind``'s provider."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE provider = %s AND name = %s AND state = 'registered' LIMIT 1",
            (kind.value, base_image),
        )
        return (await cur.fetchone()) is not None


async def _reject_config_name(conn: AsyncConnection, kind: ResourceKind, name: str) -> bool:
    """Whether a ``config``-owned row already owns ``(kind, name)`` (register must refuse it)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM resources WHERE kind = %s AND name = %s AND managed_by = %s LIMIT 1",
            (kind.value, name, ManagedBy.CONFIG.value),
        )
        return (await cur.fetchone()) is not None


async def register_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    block: str,
    name: str,
    cost_class: str,
    host_uri: str | None = None,
    base_image: str | None = None,
    concurrent_allocation_cap: int = 1,
    secret_refs: tuple[str, ...] = (),
    owner_project: str | None = None,
    probe: ResourceProbe | None = None,
    secrets_root: Path | None = None,
) -> ToolResponse:
    """Register a runtime provider resource. Requires ``platform_admin``.

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        block: The ``systems.toml`` block name: ``remote_libvirt`` / ``local_libvirt`` /
            ``fault_inject``.
        name: The stable ``(kind, name)`` identity for the new row.
        cost_class: The cost class column value.
        host_uri: The provider host URI (required for remote/local; defaulted for fault-inject).
        base_image: The registered image name (remote-libvirt only).
        concurrent_allocation_cap: The per-host concurrent-allocation cap (> 0).
        secret_refs: Credential reference strings to resolve (never their bytes).
        owner_project: The owning project; defaults to the single registering project, or pass
            ``'*'`` for a global resource.
        probe: Reachability probe port (defaults to a bounded TCP connect).
        secrets_root: Secrets root for ref resolution (defaults to ``KDIVE_SECRETS_ROOT``).

    Returns:
        A success envelope with the new resource id, or a typed failure envelope
        (authorization_denied / conflict / configuration_error).
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=REGISTER_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return denied(name, REGISTER_TOOL)

    kind = resolve_block_kind(block)
    if kind is None:
        return config_error(name, f"unsupported resource block {block!r}")
    if concurrent_allocation_cap <= 0:
        return config_error(name, "concurrent_allocation_cap must be a positive integer")

    resolved_owner = _resolve_owner_project(ctx, owner_project)
    if isinstance(resolved_owner, ToolResponse):
        return resolved_owner

    effective_host_uri = host_uri
    if kind is ResourceKind.FAULT_INJECT:
        effective_host_uri = _FAULT_INJECT_HOST_URI
    elif not (effective_host_uri and effective_host_uri.strip()):
        return config_error(name, f"{block} requires a host URI")

    probe = probe or TcpResourceProbe()
    secrets_root = secrets_root or secrets_root_from_env()

    # Non-DB preflight (bounded TCP probe + filesystem secret-ref reads) runs before any
    # connection is acquired, so a slow/unreachable host never holds a pooled connection.
    network_failure = await _network_preflight(
        kind=kind,
        name=name,
        host_uri=effective_host_uri,
        secret_refs=secret_refs,
        base_image=base_image,
        probe=probe,
        secrets_root=secrets_root,
    )
    if network_failure is not None:
        return network_failure

    return await _insert_with_preflight(
        pool,
        ctx,
        kind=kind,
        name=name,
        cost_class=cost_class,
        host_uri=effective_host_uri,
        base_image=base_image,
        cap=concurrent_allocation_cap,
        secret_refs=secret_refs,
        owner_project=resolved_owner,
    )


async def _insert_with_preflight(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    kind: ResourceKind,
    name: str,
    cost_class: str,
    host_uri: str,
    base_image: str | None,
    cap: int,
    secret_refs: tuple[str, ...],
    owner_project: str | None,
) -> ToolResponse:
    """Run the DB preflight + the guarded INSERT in one transaction; map conflicts to envelopes."""
    lease = _lease_deadline()
    caps = {CONCURRENT_ALLOCATION_CAP_KEY: cap}
    try:
        # Serialize with the inventory reconcile on the (kind, name) identity so a concurrent
        # reconcile adopt/prune of this name and this register cannot interleave (ADR-0112).
        async with (
            pool.connection() as conn,
            conn.transaction(),
            resource_identity_lock(conn, kind, name),
        ):
            failure = await _db_preflight(conn, kind=kind, name=name, base_image=base_image)
            if failure is not None:
                return failure
            resource_id = await _do_insert(
                conn,
                kind=kind,
                name=name,
                caps=caps,
                cost_class=cost_class,
                host_uri=host_uri,
                owner_project=owner_project,
                lease=lease,
            )
            await _audit_register(
                conn,
                ctx,
                kind=kind,
                name=name,
                host_uri=host_uri,
                base_image=base_image,
                cost_class=cost_class,
                cap=cap,
                secret_refs=secret_refs,
                owner_project=owner_project,
                resource_id=resource_id,
            )
    except psycopg.errors.UniqueViolation:
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFLICT,
            data={"reason": f"a {kind.value} resource named {name!r} already exists"},
        )

    _log.info(
        "runtime resource %r (%s/%s) registered by %s",
        name,
        kind.value,
        resource_id,
        ctx.principal,
    )
    return ToolResponse.success(
        str(resource_id),
        "registered",
        suggested_next_actions=["resources.list", "resources.renew"],
        data={"id": str(resource_id), "name": name, "kind": kind.value},
    )


async def _do_insert(
    conn: AsyncConnection,
    *,
    kind: ResourceKind,
    name: str,
    caps: dict[str, int],
    cost_class: str,
    host_uri: str,
    owner_project: str | None,
    lease: datetime,
) -> UUID:
    """INSERT the runtime resource row and return its id."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO resources "
            "  (kind, name, capabilities, pool, cost_class, status, host_uri, managed_by, "
            "   owner_project, lease_expires_at) "
            "VALUES (%s, %s, %s, 'default', %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                kind.value,
                name,
                Jsonb(caps),
                cost_class,
                ResourceStatus.AVAILABLE.value,
                host_uri,
                ManagedBy.RUNTIME.value,
                owner_project,
                lease,
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    resource_id = row["id"]
    assert isinstance(resource_id, UUID)
    return resource_id


async def _audit_register(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    kind: ResourceKind,
    name: str,
    host_uri: str,
    base_image: str | None,
    cost_class: str,
    cap: int,
    secret_refs: tuple[str, ...],
    owner_project: str | None,
    resource_id: UUID,
) -> None:
    """Write the register audit row (secret references only — never secret bytes)."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=REGISTER_TOOL,
            scope=f"resource:{resource_id}",
            args={
                "name": name,
                "kind": kind.value,
                "host_uri": host_uri,
                "base_image": base_image,
                "cost_class": cost_class,
                "concurrent_allocation_cap": cap,
                "secret_refs": list(secret_refs),
                "owner_project": owner_project,
            },
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )


__all__ = ["register_resource"]
