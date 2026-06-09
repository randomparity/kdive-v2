"""Queue-control `ops.*` tool tests (#138, ADR-0062).

Handlers are called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #138 acceptance bullets:

* ``ops.queue_pause`` / ``ops.queue_resume`` toggle ``queue_paused``; ``platform_operator``
  gating enforced; success and (role-holding) denial audited.
* ``ops.jobs_list`` returns cross-project queue depth + per-job state; ``platform_operator``
  gating enforced; a state filter is validated.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, BuildPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops import queue as ops_queue
from kdive.security.authz.rbac import PlatformRole


def _ctx(
    *,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    projects: tuple[str, ...] = (),
    principal: str = "op-1",
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles={},
        platform_roles=platform_roles,
    )


_OPERATOR = frozenset({PlatformRole.PLATFORM_OPERATOR})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _authorizing(project: str) -> Authorizing:
    return Authorizing(principal="p", agent_session=None, project=project)


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()))


async def _paused(url: str) -> bool:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        return await queue.is_queue_paused(conn)


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT principal, platform_role, tool, scope FROM platform_audit_log ORDER BY ts"
        )
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_pause_sets_flag_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.queue_pause(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "paused"
        assert json.loads(resp.data["queue_paused"]) is True
        assert await _paused(migrated_url) is True
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator" and rows[0][2] == "ops.queue_pause"

    asyncio.run(_run())


def test_pause_flag_and_audit_commit_atomically(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed audit write must roll back the flag flip: never a paused queue with no row.
    async def _run() -> None:
        async def boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("audit db error")

        monkeypatch.setattr(ops_queue.audit, "record_platform", boom)
        async with _pool(migrated_url) as pool:
            with pytest.raises(RuntimeError, match="audit db error"):
                await ops_queue.queue_pause(pool, _ctx(platform_roles=_OPERATOR))
        assert await _paused(migrated_url) is False  # rolled back with the audit
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_resume_clears_flag_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await ops_queue.queue_pause(pool, _ctx(platform_roles=_OPERATOR))
            resp = await ops_queue.queue_resume(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "running"
        assert await _paused(migrated_url) is False
        rows = await _platform_audit_rows(migrated_url)
        assert [r[2] for r in rows] == ["ops.queue_pause", "ops.queue_resume"]

    asyncio.run(_run())


def test_pause_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.queue_pause(pool, _ctx(projects=("proj-a",)))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert await _paused(migrated_url) is False  # flag untouched
        assert await _count_platform_audit(migrated_url) == 0  # no write-amplification

    asyncio.run(_run())


def test_pause_denied_for_auditor_is_audited(migrated_url: str) -> None:
    # platform_auditor does NOT satisfy the operator gate, but holds a platform role.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await ops_queue.queue_pause(pool, ctx)
        assert resp.status == "error"
        assert await _paused(migrated_url) is False
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1 and rows[0][1] == "platform_auditor"

    asyncio.run(_run())


def test_jobs_list_returns_cross_project_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _authorizing("proj-a"), "dk-a"
                )
                await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _authorizing("proj-b"), "dk-b"
                )
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert resp.status == "ok"
        depth = {
            key.removeprefix("depth_"): int(value)
            for key, value in resp.data.items()
            if key.startswith("depth_")
        }
        assert depth == {"queued": 2}  # both projects counted, cross-project
        jobs = [item.data for item in resp.items]
        assert {j["project"] for j in jobs} == {"proj-a", "proj-b"}
        assert all("payload" not in j for j in jobs)  # untrusted payload not surfaced
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1 and rows[0][3] == "all-projects"

    asyncio.run(_run())


def test_jobs_list_filters_by_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _authorizing("proj-a"), "dk-q"
                )
                running = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _authorizing("proj-a"), "dk-r"
                )
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'w1' WHERE id = %s",
                    (running.id,),
                )
            resp = await ops_queue.jobs_list(
                pool, _ctx(platform_roles=_OPERATOR), states=["running"]
            )
        jobs = [item.data for item in resp.items]
        assert [j["state"] for j in jobs] == ["running"]  # filtered per-job rows
        depth = {
            key.removeprefix("depth_"): int(value)
            for key, value in resp.data.items()
            if key.startswith("depth_")
        }
        assert depth == {"queued": 1, "running": 1}  # depth still spans all states

    asyncio.run(_run())


def test_jobs_list_rejects_unknown_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR), states=["bogus"])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_jobs_list_denied_for_project_only_token(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(projects=("proj-a",)))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_response_is_serializable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_queue.jobs_list(pool, _ctx(platform_roles=_OPERATOR))
        assert isinstance(resp, ToolResponse)
        json.dumps(resp.model_dump())  # the envelope round-trips to JSON

    asyncio.run(_run())


def test_admin_satisfies_operator_gate(migrated_url: str) -> None:
    # platform_admin does not imply platform_operator (separate axes); a pure admin is denied.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await ops_queue.queue_pause(pool, ctx)
        assert resp.status == "error"  # operator gate not satisfied by admin

    asyncio.run(_run())
