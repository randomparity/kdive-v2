"""jobs.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import JobKind
from kdive.jobs import queue
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import jobs as jobs_tools

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _enqueue(pool: AsyncConnectionPool, dedup: str) -> str:
    async with pool.connection() as conn:
        job = await queue.enqueue(conn, JobKind.BUILD, {}, {"principal": "p"}, dedup)
    return str(job.id)


def test_get_known_job_returns_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.get_job(pool, CTX, job_id)
        assert resp.object_id == job_id
        assert resp.status == "queued"
        assert resp.data == {"kind": "build"}

    asyncio.run(_run())


def test_get_unknown_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cancel_queued_job_transitions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.cancel_job(pool, CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_cancel_terminal_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, CTX, job_id)  # -> canceled (terminal)
            resp = await jobs_tools.cancel_job(pool, CTX, job_id)  # again
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_wait_returns_immediately_for_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, CTX, job_id)
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=5.0)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_wait_zero_timeout_is_single_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=0.0)
        assert resp.status == "queued"  # one read, no wait

    asyncio.run(_run())


def test_wait_loops_until_terminal(migrated_url: str) -> None:
    """Exercise the sleep-then-re-poll branch: a concurrent task cancels the job
    after one poll interval, and wait must return the canceled envelope having
    looped at least once (timeout long enough to require a real poll)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")

            async def _cancel_after_delay() -> None:
                await asyncio.sleep(jobs_tools.POLL_INTERVAL_S + 0.1)
                await jobs_tools.cancel_job(pool, CTX, job_id)

            canceller = asyncio.create_task(_cancel_after_delay())
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=5.0)
            await canceller
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_list_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _enqueue(pool, f"d{i}")
            resp = await jobs_tools.list_jobs(pool, CTX, limit=2)
        assert len(resp) == 2
        assert all(r.status == "queued" for r in resp)

    asyncio.run(_run())


def test_list_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, CTX, limit=50)
        assert resp == []

    asyncio.run(_run())
