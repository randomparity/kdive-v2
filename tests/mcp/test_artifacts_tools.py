"""artifacts.* tool tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.security.rbac import Role
from tests.mcp._seed import seed_crashed_system


def _ctx(projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(
        principal="u", agent_session="s", projects=projects, roles={"proj": Role.OPERATOR}
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system_with_artifacts(pool: AsyncConnectionPool) -> tuple[str, str, str]:
    """Insert a System and a sensitive + redacted artifact owned by it.

    Returns (system_id, sensitive_artifact_id, redacted_artifact_id).
    """
    sys_id = await seed_crashed_system(pool)
    ids: list[str] = []
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for name, sens in (("vmcore", "sensitive"), ("vmcore-redacted", "redacted")):
            await cur.execute(
                "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                "retention_class) VALUES ('systems', %s, %s, 'e', %s, 'vmcore') RETURNING id",
                (sys_id, f"k/systems/{sys_id}/{name}", sens),
            )
            row = await cur.fetchone()
            assert row is not None
            ids.append(str(row["id"]))
    return sys_id, ids[0], ids[1]


def test_artifacts_list_returns_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp}
        assert ids == {red_id}  # the sensitive row is never surfaced

    asyncio.run(_run())


def test_artifacts_get_redacted_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id=red_id)
        assert resp.status != "error" and resp.refs

    asyncio.run(_run())


def test_artifacts_get_sensitive_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id=sens_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_get_cross_project_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_get(
                pool, _ctx(projects=("other",)), artifact_id=red_id
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_list_cross_project_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_list(
                pool, _ctx(projects=("other",)), system_id=sys_id
            )
        assert resp == []

    asyncio.run(_run())
