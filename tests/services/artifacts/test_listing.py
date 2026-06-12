"""Behavior tests for redacted artifact listing service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import Sensitivity
from kdive.mcp.auth import RequestContext
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.artifacts.listing import RedactedArtifact, list_redacted_system_artifacts
from tests.mcp._seed import seed_crashed_system

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(
    *, projects: tuple[str, ...] = ("proj",), role: Role | None = Role.VIEWER
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


async def _artifact(
    pool: AsyncConnectionPool,
    system_id: str,
    name: str,
    *,
    sensitivity: Sensitivity = Sensitivity.REDACTED,
    created_offset: timedelta = timedelta(0),
) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO artifacts "
            "(created_at, updated_at, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES (%s, %s, 'systems', %s, %s, 'e', %s, 'console') "
            "RETURNING id",
            (
                _DT + created_offset,
                _DT + created_offset,
                system_id,
                f"k/systems/{system_id}/{name}",
                sensitivity.value,
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row["id"])


def test_listing_returns_authorized_redacted_artifacts_newest_first(migrated_url: str) -> None:
    async def _run() -> tuple[list[str], list[str], str, str]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            older = await _artifact(pool, system_id, "older", created_offset=timedelta(minutes=1))
            await _artifact(pool, system_id, "raw", sensitivity=Sensitivity.SENSITIVE)
            await _artifact(pool, system_id, "quarantine", sensitivity=Sensitivity.QUARANTINED)
            newer = await _artifact(pool, system_id, "newer", created_offset=timedelta(minutes=2))

            listed = await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id)

        return [item.id for item in listed], [item.object_key for item in listed], newer, older

    ids, keys, newer_id, older_id = asyncio.run(_run())
    assert ids == [newer_id, older_id]
    assert keys[0].endswith("/newer")
    assert keys[1].endswith("/older")


def test_listing_hides_invalid_missing_and_foreign_system_ids(migrated_url: str) -> None:
    async def _run() -> tuple[
        list[RedactedArtifact], list[RedactedArtifact], list[RedactedArtifact]
    ]:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool, project="other")
            invalid = await list_redacted_system_artifacts(pool, _ctx(), system_id="not-a-uuid")
            missing = await list_redacted_system_artifacts(
                pool,
                _ctx(),
                system_id="00000000-0000-0000-0000-000000000000",
            )
            foreign = await list_redacted_system_artifacts(pool, _ctx(), system_id=system_id)
        return invalid, missing, foreign

    assert asyncio.run(_run()) == ([], [], [])


def test_listing_requires_viewer_role_for_project_member(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            with pytest.raises(AuthorizationError):
                await list_redacted_system_artifacts(pool, _ctx(role=None), system_id=system_id)

    asyncio.run(_run())
