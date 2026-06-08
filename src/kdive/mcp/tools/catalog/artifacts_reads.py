"""Redacted-only artifact list/get/search handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, LiteralString, NamedTuple, Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from kdive.domain.errors import CategorizedError
from kdive.domain.models import Sensitivity
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security.artifacts.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.artifact_listing import RedactedArtifact, list_redacted_system_artifacts
from kdive.store.objectstore import (
    FetchedArtifact,
    HeadResult,
    object_store_from_env,
)

_log = logging.getLogger(__name__)

_MAX_SEARCHABLE_ARTIFACT_BYTES = 1024 * 1024
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


class ArtifactSearchRequest(BaseModel):
    """Request fields for bounded literal search in one redacted artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: Annotated[str, Field(description="The redacted System artifact id.")]
    pattern: Annotated[
        str,
        Field(description="Literal OR search pattern, e.g. '__d_lookup' or 'panic'."),
    ]
    before_lines: Annotated[int, Field(description="Context lines before each match.")] = 2
    after_lines: Annotated[int, Field(description="Context lines after each match.")] = 4
    max_matches: Annotated[int, Field(description="Maximum match windows to return.")] = 20


@dataclass(frozen=True, slots=True)
class ArtifactReadHandlers:
    """Artifact read handlers with the object-store search seam bound at construction."""

    search_store_factory: Callable[[], _SearchStore] = object_store_from_env

    async def artifacts_search_text(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        request: ArtifactSearchRequest,
    ) -> ToolResponse:
        return await _artifacts_search_text(
            pool,
            ctx,
            request=request,
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
        _artifact_list_items(await list_redacted_system_artifacts(pool, ctx, system_id=system_id)),
        suggested_next_actions=["artifacts.get"],
    )


def _artifact_list_items(artifacts: list[RedactedArtifact]) -> list[ToolResponse]:
    """Return redacted artifact item envelopes."""
    responses: list[ToolResponse] = []
    for artifact in artifacts:
        try:
            responses.append(
                ToolResponse.success(
                    artifact.id,
                    "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": artifact.object_key},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", artifact.id)
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
    request: ArtifactSearchRequest,
    store: _SearchStore,
) -> ToolResponse:
    """Search one redacted System-owned text artifact with bounded literal context."""
    artifact_id = request.artifact_id
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    try:
        parse_literal_terms(request.pattern)
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
            pattern=request.pattern,
            before_lines=request.before_lines,
            after_lines=request.after_lines,
            max_matches=request.max_matches,
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
