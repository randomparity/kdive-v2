"""``ops.reconcile_systems`` — trigger one inventory reconcile pass over MCP (M2.6 #399).

The inventory engine (``systems.toml`` → ``image_catalog`` / ``resources`` / ``build_hosts``)
runs from three places: the ``kdive reconcile-systems`` CLI, the reconciler-loop drift pass, and
this on-demand MCP trigger. Unlike ``ops.reconcile_now`` (gated ``platform_operator``), this pass
can **prune** config rows that left the file (and the row-delete frees an image's S3 bytes to the
existing GC), so it is gated tighter at ``platform_admin`` — a dedicated tool rather than widening
``reconcile_now``'s contract (ADR-0112).

Audit (destructive-tier): a successful pass writes one ``platform_audit_log`` row recording the
actor and the resulting :class:`~kdive.inventory.reconcile.ReconcileDiff`. The pruned/cordoned
identities go in the human-readable ``scope`` (so a config-driven deletion is directly
attributable) and the whole diff is committed to ``args_digest`` (tamper-evident). No secret bytes
are recorded — the diff carries only ``(kind, name)`` identities.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import SYSTEMS_TOML
from kdive.domain.errors import ErrorCategory
from kdive.inventory import InventoryError, load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff, ReconcileRecord
from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images
from kdive.inventory.reconcile_resources import reconcile_resources
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.services.images.retention import ImageSweepStore

_log = logging.getLogger(__name__)

_RECONCILE_TOOL = "ops.reconcile_systems"
_RECONCILE_OBJECT_ID = "reconcile-systems"
# A control action over every system/inventory row, not one object (mirrors reconcile_now's scope).
_BASE_SCOPE = "all-systems"


class _AbsentImageStore:
    """A no-op ``ImageHeadStore`` used when no object store is configured.

    Every HEAD reports absent, so an ``s3`` image stays ``defined`` + warns (exactly the
    store-down degrade the engine already tolerates), while ``staged`` images, resources, and
    build hosts reconcile normally — the inventory pass needs no S3 to do most of its work.
    """

    def head_present(self, key: str) -> bool:  # noqa: ARG002 - protocol param name, unused
        return False


def _resolve_path() -> Path:
    """Resolve the inventory file path from ``KDIVE_SYSTEMS_TOML`` (default ``./systems.toml``)."""
    raw = config.get(SYSTEMS_TOML)
    return Path(raw) if raw is not None else Path("./systems.toml")


async def reconcile_systems(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    image_store: ImageSweepStore | None = None,
) -> ToolResponse:
    """Run one inventory reconcile pass on demand; audit and return the combined diff.

    Gates ``platform_admin`` first (a denial writes no inventory change), reconciles the
    ``systems.toml`` into ``image_catalog`` / ``resources`` / ``build_hosts``, audits the action
    to ``platform_audit_log`` (actor + diff), and returns the per-category counts and the
    pruned/cordoned identities.

    Args:
        pool: The shared async pool the pass draws a fresh connection from.
        ctx: The caller's request context; must hold ``platform_admin``.
        image_store: The object store HEADed to confirm ``s3`` image existence, or ``None`` to
            run with a no-op store (``s3`` images stay ``defined`` + warn; staged/resources/build
            hosts still reconcile).

    Returns:
        A success ``ToolResponse`` carrying the diff, or a
        ``ToolResponse.failure(AUTHORIZATION_DENIED)`` when the caller lacks the role.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_RECONCILE_TOOL,
                scope=_BASE_SCOPE,
                args={"tool": _RECONCILE_TOOL},
            )
            return ToolResponse.failure(
                _RECONCILE_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_RECONCILE_TOOL],
            )
        store: ImageHeadStore = image_store if image_store is not None else _AbsentImageStore()
        try:
            diff = await _run_pass(pool, store)
        except InventoryError as exc:
            _log.warning("ops.reconcile_systems: systems.toml is malformed: %s", exc)
            return _categorized_failure(exc)
        await _audit_pass(pool, ctx, diff)
        return _response(diff)


async def _run_pass(pool: AsyncConnectionPool, store: ImageHeadStore) -> ReconcileDiff:
    """Reconcile the inventory file into the catalog and return one merged ``ReconcileDiff``.

    An absent default file is a quiet no-op (an empty diff); a present-but-malformed file raises
    :class:`~kdive.inventory.InventoryError`, surfaced as a categorized failure to the caller.
    """
    doc = _load()
    merged = ReconcileDiff()
    if doc is None:
        return merged
    async with pool.connection() as conn:
        _extend(merged, await reconcile_images(conn, doc, store))
        _extend(merged, await reconcile_resources(conn, doc))
        _extend(merged, await reconcile_build_hosts(conn, doc))
    return merged


def _load() -> InventoryDoc | None:
    """Load the inventory doc from the default path; an absent file returns ``None``."""
    return load_inventory_optional(_resolve_path())


def _extend(into: ReconcileDiff, part: ReconcileDiff) -> None:
    """Fold one per-entity diff into the merged diff."""
    into.created.extend(part.created)
    into.updated.extend(part.updated)
    into.pruned.extend(part.pruned)
    into.cordoned.extend(part.cordoned)
    into.warned.extend(part.warned)


def _names(records: list[ReconcileRecord]) -> list[str]:
    return [r.name for r in records]


async def _audit_pass(pool: AsyncConnectionPool, ctx: RequestContext, diff: ReconcileDiff) -> None:
    """Write the destructive-tier audit row: actor + diff, prunes/cordons attributable in scope."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_RECONCILE_TOOL,
                scope=_audit_scope(diff),
                args=_audit_args(diff),
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def _audit_scope(diff: ReconcileDiff) -> str:
    """A human-readable, queryable scope naming the pruned/cordoned identities (attribution)."""
    parts = [_BASE_SCOPE]
    if diff.pruned:
        parts.append(f"pruned={','.join(_names(diff.pruned))}")
    if diff.cordoned:
        parts.append(f"cordoned={','.join(_names(diff.cordoned))}")
    return " ".join(parts)


def _audit_args(diff: ReconcileDiff) -> dict[str, object]:
    """The full diff committed to ``args_digest`` (tamper-evident; identities only, no secrets)."""
    return {
        "tool": _RECONCILE_TOOL,
        "created": _names(diff.created),
        "updated": _names(diff.updated),
        "pruned": _names(diff.pruned),
        "cordoned": _names(diff.cordoned),
        "warned": _names(diff.warned),
    }


def _response(diff: ReconcileDiff) -> ToolResponse:
    """Render the merged diff as a per-category count summary + the changed identities."""
    return ToolResponse.success(
        _RECONCILE_OBJECT_ID,
        "ok",
        suggested_next_actions=[_RECONCILE_TOOL, "resources.list"],
        data={
            "created": str(len(diff.created)),
            "updated": str(len(diff.updated)),
            "pruned": str(len(diff.pruned)),
            "cordoned": str(len(diff.cordoned)),
            "warned": str(len(diff.warned)),
            "pruned_names": ",".join(_names(diff.pruned)),
            "cordoned_names": ",".join(_names(diff.cordoned)),
        },
    )


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, image_store: ImageSweepStore | None
) -> None:
    """Register ``ops.reconcile_systems`` (platform_admin, destructive-tier)."""

    @app.tool(
        name=_RECONCILE_TOOL,
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def ops_reconcile_systems() -> ToolResponse:
        """Reconcile systems.toml into the catalog (can prune). Platform admin."""
        return await reconcile_systems(pool, current_context(), image_store=image_store)


def _categorized_failure(exc: InventoryError) -> ToolResponse:
    """Map an InventoryError (malformed file) to a configuration-error envelope."""
    return ToolResponse.failure(
        _RECONCILE_OBJECT_ID,
        ErrorCategory.CONFIGURATION_ERROR,
        data={"reason": str(exc)},
    )


__all__ = ["reconcile_systems", "register"]
