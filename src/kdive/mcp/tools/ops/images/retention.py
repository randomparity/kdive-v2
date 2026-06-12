"""``images.prune_expired`` and ``images.extend`` break-glass workflows."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import IMAGE_PRIVATE_LIFETIME_MAX
from kdive.domain.models import ImageVisibility
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.ops.images._common import (
    EXTEND_TOOL,
    PRUNE_OBJECT_ID,
    PRUNE_SCOPE,
    PRUNE_TOOL,
    audit_platform_denial,
    blank,
    denied,
    record_admin_breakglass,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.services.images.retention import ImageSweepStore, repair_expired_private_images

_log = logging.getLogger(__name__)


async def prune_expired(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    reason: str,
    image_store: ImageSweepStore,
) -> ToolResponse:
    """Force the expired-private-image retention sweep now. Requires ``platform_admin``."""
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
        except AuthorizationError:
            await audit_platform_denial(
                pool, ctx, tool=PRUNE_TOOL, scope=f"denied:{PRUNE_SCOPE}", args={}
            )
            return denied(PRUNE_OBJECT_ID, PRUNE_TOOL)
        if blank(reason):
            return _config_error(PRUNE_OBJECT_ID)
        await record_admin_breakglass(
            pool, ctx, tool=PRUNE_TOOL, scope=PRUNE_SCOPE, args={"reason": reason}
        )
        async with pool.connection() as conn:
            pruned = await repair_expired_private_images(conn, image_store)
        _log.warning("break-glass prune of %d expired private images by %s", pruned, ctx.principal)
        return ToolResponse.success(PRUNE_OBJECT_ID, "pruned", data={"pruned": str(pruned)})


def _ceiling(now: datetime) -> datetime:
    """The per-image lifetime ceiling: ``now`` plus the configured maximum lifetime."""
    return now + timedelta(seconds=config.require(IMAGE_PRIVATE_LIFETIME_MAX))


async def extend(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    image_id: str,
    seconds: int,
    reason: str,
) -> ToolResponse:
    """Re-arm a private image's ``expires_at`` under the per-row lock. Requires ``platform_admin``.

    Gates ``platform_admin`` first, records the break-glass row, then sets
    ``expires_at = now() + seconds`` clamped to the lifetime ceiling under the same ``FOR UPDATE``
    lock the reconciler's extend fence honors. ``seconds`` must be positive.
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _config_error(image_id)
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=EXTEND_TOOL,
                scope=f"denied:{image_id}",
                args={"image_id": image_id},
            )
            return denied(image_id, EXTEND_TOOL)
        if blank(reason) or seconds <= 0:
            return _config_error(image_id)
        await record_admin_breakglass(
            pool,
            ctx,
            tool=EXTEND_TOOL,
            scope=f"image:{image_id}",
            args={"image_id": image_id, "seconds": str(seconds), "reason": reason},
        )
        return await _rearm_expiry(pool, uid, seconds=seconds)


async def _rearm_expiry(pool: AsyncConnectionPool, uid: UUID, *, seconds: int) -> ToolResponse:
    """Set the private image's ``expires_at`` to the clamped deadline under its row lock."""
    async with (
        pool.connection() as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM image_catalog "
            "WHERE id = %s AND visibility = %s AND expires_at IS NOT NULL FOR UPDATE",
            (uid, ImageVisibility.PRIVATE.value),
        )
        if await cur.fetchone() is None:
            return _config_error(str(uid))
        now = datetime.now(UTC)
        requested = now + timedelta(seconds=seconds)
        deadline = min(requested, _ceiling(now))
        await cur.execute("UPDATE image_catalog SET expires_at = %s WHERE id = %s", (deadline, uid))
    return ToolResponse.success(str(uid), "extended", data={"expires_at": deadline.isoformat()})
