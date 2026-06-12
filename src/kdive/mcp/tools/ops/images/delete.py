"""``images.delete`` project-private image workflow."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ImageVisibility
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.ops.images._common import (
    DELETE_TOOL,
    OBJECT_KIND,
    audit_project_denial,
    denied,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, Role, RoleDenied, require_role
from kdive.services.images.retention import image_referenced_by_live_system

_log = logging.getLogger(__name__)


async def delete(pool: AsyncConnectionPool, ctx: RequestContext, *, image_id: str) -> ToolResponse:
    """Delete a project-private catalog image. Requires ``operator`` on the image's project.

    Resolves the image's owning project from the row, then gates ``operator`` on it. A
    member-over-reach or cross-project caller is denied and audited before the row is touched; the
    catalog row survives. Cross-project force-delete is deliberately not exposed here.
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _config_error(image_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            entry = await IMAGE_CATALOG.get(conn, uid)
        if entry is None:
            return _config_error(image_id)
        if entry.visibility is not ImageVisibility.PRIVATE or entry.owner is None:
            return _config_error(image_id)
        try:
            require_role(ctx, entry.owner, Role.OPERATOR)
        except RoleDenied:
            await audit_project_denial(
                pool, ctx, tool=DELETE_TOOL, project=entry.owner, args={"image_id": image_id}
            )
            return denied(image_id, DELETE_TOOL)
        except AuthorizationError:
            return denied(image_id, DELETE_TOOL)
        return await _delete_owned(pool, ctx, uid, project=entry.owner)


async def _delete_owned(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    """Reference-guard, then delete the row + audit, all under the row's ``FOR UPDATE`` lock."""
    async with (
        pool.connection() as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM image_catalog WHERE id = %s AND visibility = %s FOR UPDATE",
            (uid, ImageVisibility.PRIVATE.value),
        )
        if await cur.fetchone() is None:
            return ToolResponse.success(str(uid), "deleted")
        if await image_referenced_by_live_system(cur, uid):
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.CONFIGURATION_ERROR,
                data={"reason": "image is referenced by a non-terminal System"},
            )
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (uid,))
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=DELETE_TOOL,
                object_kind=OBJECT_KIND,
                object_id=uid,
                transition="deleted",
                args={"image_id": str(uid)},
                project=project,
            ),
        )
    _log.info("operator %s deleted private image %s in project %s", ctx.principal, uid, project)
    return ToolResponse.success(str(uid), "deleted")
