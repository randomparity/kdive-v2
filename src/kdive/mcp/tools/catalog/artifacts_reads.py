"""Redacted-only artifact list/get/search handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import LiteralString, NamedTuple, Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError
from kdive.domain.models import Sensitivity
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)
from kdive.security.context import RequestContext
from kdive.security.rbac import Role, require_role
from kdive.store.objectstore import (
    FetchedArtifact,
    HeadResult,
    object_store_from_env,
)

_log = logging.getLogger(__name__)

_MAX_SEARCHABLE_ARTIFACT_BYTES = 1024 * 1024
_LIST_SQL: LiteralString = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = 'redacted' "
    "ORDER BY created_at DESC"
)
_GET_SQL: LiteralString = (
    "SELECT id, object_key, owner_id FROM artifacts "
    "WHERE id = %s AND owner_kind = 'systems' AND sensitivity = 'redacted'"
)
_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


class _SearchStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


class _AuthorizedArtifact(NamedTuple):
    key: str


@dataclass(frozen=True, slots=True)
class ArtifactReadHandlers:
    """Artifact read handlers with the object-store search seam bound at construction."""

    search_store_factory: Callable[[], _SearchStore] = object_store_from_env

    async def artifacts_search_text(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        artifact_id: str,
        pattern: str,
        before_lines: int = 2,
        after_lines: int = 4,
        max_matches: int = 20,
    ) -> ToolResponse:
        return await _artifacts_search_text(
            pool,
            ctx,
            artifact_id=artifact_id,
            pattern=pattern,
            before_lines=before_lines,
            after_lines=after_lines,
            max_matches=max_matches,
            store=self.search_store_factory(),
        )


async def _authorized_redacted_artifact(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> _AuthorizedArtifact | ToolResponse:
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid,))
            row = await cur.fetchone()
            if row is None:
                return _config_error(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _config_error(artifact_id)
        require_role(ctx, owner["project"], Role.VIEWER)
        return _AuthorizedArtifact(key=str(row["object_key"]))


async def artifacts_list(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Return the System's `redacted` artifacts in one collection envelope."""
    return ToolResponse.collection(
        system_id,
        "ok",
        await artifact_list_items(pool, ctx, system_id=system_id),
        suggested_next_actions=["artifacts.get"],
    )


async def artifact_list_items(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[ToolResponse]:
    """Return redacted artifact item envelopes; absent systems produce an empty list."""
    uid = _as_uuid(system_id)
    if uid is None:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            require_role(ctx, owner["project"], Role.VIEWER)
            await cur.execute(_LIST_SQL, (uid,))
            rows = await cur.fetchall()
    responses: list[ToolResponse] = []
    for row in rows:
        try:
            responses.append(
                ToolResponse.success(
                    str(row["id"]),
                    "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": row["object_key"]},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", row["id"])
    return responses


async def artifacts_get(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> ToolResponse:
    """Return one `redacted` artifact's envelope, or a not-found-shaped config error."""
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    return ToolResponse.success(
        artifact_id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs={"object": authorized.key},
    )


async def _artifacts_search_text(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    artifact_id: str,
    pattern: str,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
    store: _SearchStore,
) -> ToolResponse:
    """Search one redacted System-owned text artifact with bounded literal context."""
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    try:
        parse_literal_terms(pattern)
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    key = authorized.key
    try:
        head = await asyncio.to_thread(store.head, key)
    except CategorizedError as exc:
        return ToolResponse.failure(artifact_id, exc.category)
    if head is None:
        return _config_error(artifact_id)
    if head.size_bytes > _MAX_SEARCHABLE_ARTIFACT_BYTES:
        return _config_error(
            artifact_id,
            data={"reason": "artifact_too_large", "size_bytes": str(head.size_bytes)},
        )
    try:
        fetched = await asyncio.to_thread(store.get_artifact, key, head.etag)
        if fetched.sensitivity is not Sensitivity.REDACTED:
            return _config_error(artifact_id)
        result = search_text(
            fetched.data,
            pattern=pattern,
            before_lines=before_lines,
            after_lines=after_lines,
            max_matches=max_matches,
        )
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    except CategorizedError as exc:
        return ToolResponse.failure(artifact_id, exc.category)
    return ToolResponse.success(
        artifact_id,
        "searched",
        suggested_next_actions=["artifacts.search_text", "runs.get"],
        refs={"artifact": key},
        data={
            "match_count": str(result.match_count),
            "truncated": str(result.truncated).lower(),
            "matches_json": result.matches_json(),
        },
    )
