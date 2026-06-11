"""``images.list`` read tool: the RBAC-filtered catalog view (M2.4/7, ADR-0092/0093).

The ``kdivectl images list`` server seam. A caller sees every ``public`` catalog row plus the
``private`` rows owned by the projects granted to their token, and never another project's
private image. The filter is applied **in SQL** (a parameterized ``owner = ANY`` over the
granted set) so an ungranted private row never leaves the database. Unlike
:func:`kdive.images.catalog.resolve_rootfs` (which returns only the one bootable ``registered``
row), the operator list surfaces every state — a ``defined`` baseline and a ``pending`` publish
included — so the operator can see in-flight and seeded images.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ImageCatalogEntry, ImageVisibility
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext

_LIST_TOOL = "images.list"

_LIST_SQL = """
    SELECT *
    FROM image_catalog
    WHERE visibility = %(public)s
       OR (visibility = %(private)s AND owner = ANY(%(projects)s))
    ORDER BY provider, name, arch
"""


def _row_envelope(entry: ImageCatalogEntry) -> ToolResponse:
    """One image row as a sub-envelope: identity, scope, and publish state in ``data``."""
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={
            "provider": entry.provider,
            "name": entry.name,
            "arch": entry.arch,
            "visibility": entry.visibility.value,
            "owner": entry.owner or "",
            "state": entry.state.value,
        },
    )


async def list_images(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """List the public catalog images plus the caller's projects' private images.

    The private filter is parameterized on the caller's granted project set, so a private row
    owned by an ungranted project is never selected. Requires only a verified token (an
    authenticated principal) — image visibility, not a role, gates what is returned.
    """
    with bind_context(principal=ctx.principal):
        params = {
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": list(ctx.projects),
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LIST_SQL, params)
            rows = await cur.fetchall()
    items = [_row_envelope(ImageCatalogEntry.model_validate(row)) for row in rows]
    return ToolResponse.collection("images", "ok", items, suggested_next_actions=[_LIST_TOOL])


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``images.list`` read tool on ``app``, bound to ``pool``."""

    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def images_list() -> ToolResponse:
        """List catalog images visible to the caller (public + own-project private). Auth only."""
        return await list_images(pool, current_context())
