"""artifacts.* tool tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog.artifacts_reads import (
    ArtifactReadHandlers,
    ArtifactSearchRequest,
    artifacts_get,
    artifacts_list,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.store.objectstore import FetchedArtifact, HeadResult
from tests.mcp._seed import seed_crashed_system


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


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


class _SearchStore:
    def __init__(
        self,
        data: bytes,
        *,
        size: int | None = None,
        sensitivity: Sensitivity = Sensitivity.REDACTED,
        head_error: CategorizedError | None = None,
        get_error: CategorizedError | None = None,
        missing_head: bool = False,
    ) -> None:
        self.data = data
        self.size = len(data) if size is None else size
        self.sensitivity = sensitivity
        self.head_error = head_error
        self.get_error = get_error
        self.missing_head = missing_head
        self.headed = False
        self.got = False

    def head(self, key: str) -> HeadResult | None:
        self.headed = True
        if self.head_error is not None:
            raise self.head_error
        if self.missing_head:
            return None
        return HeadResult(size_bytes=self.size, checksum_sha256=None, etag="e")

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.got = True
        if self.get_error is not None:
            raise self.get_error
        assert etag == "e"
        return FetchedArtifact(self.data, self.sensitivity, "console")


def _artifact_read_handlers(store: _SearchStore) -> ArtifactReadHandlers:
    return ArtifactReadHandlers(lambda: store)


def _search_request(
    artifact_id: str,
    pattern: str,
    *,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
) -> ArtifactSearchRequest:
    return ArtifactSearchRequest(
        artifact_id=artifact_id,
        pattern=pattern,
        before_lines=before_lines,
        after_lines=after_lines,
        max_matches=max_matches,
    )


def test_artifacts_list_returns_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp.collection_items()}
        assert ids == {red_id}  # the sensitive row is never surfaced

    asyncio.run(_run())


def test_artifacts_search_text_returns_bounded_matches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"before\nRIP: __d_lookup+0x1\nafter\n")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool,
                _ctx(),
                request=_search_request(
                    red_id,
                    "__d_lookup|Oops",
                    before_lines=1,
                    after_lines=1,
                ),
            )
        assert resp.status == "searched"
        assert resp.data["match_count"] == "1"
        matches = json.loads(resp.data["matches_json"])
        assert matches[0]["line"] == 2
        assert matches[0]["before"] == ["before"]
        assert matches[0]["after"] == ["after"]

    asyncio.run(_run())


def test_artifacts_search_text_sensitive_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await _artifact_read_handlers(_SearchStore(b"panic")).artifacts_search_text(
                pool,
                _ctx(),
                request=_search_request(sens_id, "panic"),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_search_text_requires_viewer(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await _artifact_read_handlers(_SearchStore(b"panic")).artifacts_search_text(
                    pool, _ctx(role=None), request=_search_request(red_id, "panic")
                )

    asyncio.run(_run())


def test_artifacts_search_text_rejects_oversized_before_get(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"", size=1024 * 1024 + 1)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "artifact_too_large"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_missing_store_head_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic", missing_head=True)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_maps_store_head_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                head_error=CategorizedError(
                    "store down", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                ),
            )
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_rejects_non_redacted_fetch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic", sensitivity=Sensitivity.SENSITIVE)
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is True

    asyncio.run(_run())


def test_artifacts_search_text_maps_store_get_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(
                b"panic",
                get_error=CategorizedError("stale", category=ErrorCategory.STALE_HANDLE),
            )
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"

    asyncio.run(_run())


def test_artifacts_search_text_rejects_bad_pattern_before_head(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(red_id, "a||b")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "bad_search_input"
        assert store.headed is False
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_list_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await artifacts_list(pool, _ctx(role=None), system_id=sys_id)

    asyncio.run(_run())


def test_artifacts_get_redacted_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(), artifact_id=red_id)
        assert resp.status != "error" and resp.refs

    asyncio.run(_run())


def test_artifacts_get_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await artifacts_get(pool, _ctx(role=None), artifact_id=red_id)

    asyncio.run(_run())


def test_artifacts_get_sensitive_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(), artifact_id=sens_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_get_cross_project_is_not_found_shaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(pool, _ctx(projects=("other",)), artifact_id=red_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_get(pool, _ctx(), artifact_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_list_cross_project_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_list(pool, _ctx(projects=("other",)), system_id=sys_id)
        assert resp.collection_items() == []

    asyncio.run(_run())


def test_artifacts_list_malformed_system_id_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await artifacts_list(pool, _ctx(), system_id="not-a-uuid")
        assert resp.collection_items() == []

    asyncio.run(_run())
