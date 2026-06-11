"""``images.list`` read tool: RBAC-filtered to public + caller's projects' private rows.

The read tool is the ``kdivectl images list`` server seam. A caller sees every public
catalog image plus the private images owned by the projects granted to their token, and
never another project's private image (the isolation the spec's exit criterion 3 asserts).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.catalog import images as catalog_images
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role


def _ctx(*projects: str) -> RequestContext:
    return RequestContext(
        principal="dev-1",
        agent_session="sess-1",
        projects=tuple(projects),
        roles={p: Role.VIEWER for p in projects},
        platform_roles=frozenset(),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _insert(
    pool: AsyncConnectionPool,
    *,
    name: str,
    visibility: str,
    owner: str | None,
    state: str = "registered",
) -> None:
    # A `defined` row is object-less by design (image_object_present CHECK); only a
    # built (pending/registered) row carries an object_key + digest.
    key = None if state == "defined" else f"images/local-libvirt/{name}/x86_64.qcow2"
    digest = None if state == "defined" else "sha256:abc"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
            " expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " %(digest)s, %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " %(state)s, now())",
            {
                "name": name,
                "key": key,
                "digest": digest,
                "vis": visibility,
                "owner": owner,
                "state": state,
            },
        )


def _names(resp: object) -> set[str]:
    items = getattr(resp, "items", [])
    return {str(item.data["name"]) for item in items}


def test_list_returns_public_and_own_private_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="fedora", visibility="public", owner=None)
            await _insert(pool, name="mine", visibility="private", owner="proj-a")
            await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            resp = await catalog_images.list_images(pool, _ctx("proj-a"))
        assert resp.status == "ok"
        assert _names(resp) == {"fedora", "mine"}

    asyncio.run(_run())


def test_list_excludes_other_projects_private(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="theirs", visibility="private", owner="proj-b")
            resp = await catalog_images.list_images(pool, _ctx("proj-a"))
        assert _names(resp) == set()

    asyncio.run(_run())


def test_list_no_projects_sees_only_public(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="fedora", visibility="public", owner=None)
            await _insert(pool, name="mine", visibility="private", owner="proj-a")
            resp = await catalog_images.list_images(pool, _ctx())
        assert _names(resp) == {"fedora"}

    asyncio.run(_run())


def test_list_includes_pending_and_defined_states(migrated_url: str) -> None:
    # The operator list surfaces every catalog row regardless of publish state (a
    # defined baseline / a pending publish), unlike resolve_rootfs which returns only
    # registered rows.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert(pool, name="baseline", visibility="public", owner=None, state="defined")
            resp = await catalog_images.list_images(pool, _ctx())
        assert "baseline" in _names(resp)

    asyncio.run(_run())
