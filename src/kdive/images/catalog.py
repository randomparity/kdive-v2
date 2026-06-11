"""Async image-catalog resolver (ADR-0092, ADR-0093).

Replaces the synchronous YAML rootfs lookup. Resolution returns one ``registered`` image
visible to the calling project: a same-name project-private image shadows the public one
(``private`` first), so the result is deterministic. ``defined`` and ``pending`` rows are never
returned — only a fully published (``registered``) image is bootable.
"""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility

# Order by visibility so the project's private row (if any) sorts before the public one; the
# resolver takes the first. `private` < `public` lexically, so the explicit CASE keeps the
# intent legible and independent of the enum spelling.
_RESOLVE_SQL = """
    SELECT *
    FROM image_catalog
    WHERE provider = %(provider)s
      AND name = %(name)s
      AND state = %(registered)s
      AND (visibility = %(public)s OR (visibility = %(private)s AND owner = %(project)s))
    ORDER BY CASE WHEN visibility = %(private)s THEN 0 ELSE 1 END
    LIMIT 1
"""


async def resolve_rootfs(
    conn: AsyncConnection, provider: str, name: str, *, project: str
) -> ImageCatalogEntry | None:
    """Resolve one registered rootfs image visible to ``project``.

    Returns the project's private image first (private shadows public on the same
    ``(provider, name)``); otherwise the public image; else ``None``. Only ``registered``
    rows are returned, so a ``defined``- or ``pending``-only baseline resolves to ``None``.

    Args:
        conn: An async Postgres connection.
        provider: The provider key (e.g. ``local-libvirt``).
        name: The catalog image name.
        project: The owning project the caller resolves on behalf of.

    Returns:
        The resolved :class:`ImageCatalogEntry`, or ``None`` if no registered image is visible.
    """
    params = {
        "provider": provider,
        "name": name,
        "registered": ImageState.REGISTERED.value,
        "public": ImageVisibility.PUBLIC.value,
        "private": ImageVisibility.PRIVATE.value,
        "project": project,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RESOLVE_SQL, params)
        row = await cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)
