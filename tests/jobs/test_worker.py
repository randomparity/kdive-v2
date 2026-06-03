"""Tests for the worker claim/dispatch loop (ADR-0018)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker


async def _final_state(url: str, job_id: UUID) -> Job:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        job = await JOBS.get(conn, job_id)
    assert job is not None
    return job


def _unopened_pool() -> AsyncConnectionPool:
    """A type-correct pool that never connects — the construct guard runs before use."""
    return AsyncConnectionPool("postgresql://localhost/unused", open=False)


def test_init_rejects_interval_above_third_of_lease() -> None:
    with pytest.raises(ValueError, match="heartbeat_interval"):
        Worker(
            _unopened_pool(),
            HandlerRegistry(),
            worker_id="w1",
            lease=timedelta(seconds=3),
            heartbeat_interval=timedelta(seconds=2),
        )


def test_init_accepts_interval_at_third_of_lease() -> None:
    Worker(
        _unopened_pool(),
        HandlerRegistry(),
        worker_id="w1",
        lease=timedelta(seconds=3),
        heartbeat_interval=timedelta(seconds=1),
    )


def test_run_once_happy_path(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls: list[Job] = []

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                calls.append(job)
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-happy")

            processed = await worker.run_once()
            assert processed is not None and processed.id == job.id
            assert len(calls) == 1
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED
            assert final.result_ref == "s3://out"

            assert await worker.run_once() is None  # queue now empty

    asyncio.run(_run())


def test_run_once_unknown_kind_dead_letters(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            worker = Worker(pool, HandlerRegistry(), worker_id="w1")  # no handlers
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-unk")
            await worker.run_once()
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.error_category is ErrorCategory.NOT_IMPLEMENTED
            assert final.attempt == 1  # claimed once, dead-lettered at once (terminal)

    asyncio.run(_run())


def test_run_once_dedup_runs_handler_once(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                first = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dedup")
                second = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dedup")
            assert second.id == first.id

            await worker.run_once()
            assert await worker.run_once() is None  # only one job ever existed
            assert calls == 1

    asyncio.run(_run())


def test_run_once_dead_letters_after_max_attempts(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def always_raises(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                raise CategorizedError("boom", category=ErrorCategory.BUILD_FAILURE)

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, always_raises)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn, JobKind.BUILD, {}, {"p": "a"}, "dk-poison", max_attempts=3
                )

            for _ in range(3):
                await worker.run_once()
            assert calls == 3
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.error_category is ErrorCategory.BUILD_FAILURE
            assert await worker.run_once() is None  # dead-lettered: not re-dequeued

    asyncio.run(_run())


def test_run_once_reclaims_lapsed_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            calls = 0

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                nonlocal calls
                calls += 1
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-lapse")
                # Simulate a dead worker holding a now-lapsed lease.
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'dead', "
                    "lease_expires_at = now() - interval '1 min' WHERE id = %s",
                    (job.id,),
                )
            processed = await worker.run_once()  # reclaims and runs it
            assert processed is not None and processed.id == job.id
            assert calls == 1
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED
            assert final.attempt == 1  # 0 -> 1 on reclaim (the dead worker never charged it)

    asyncio.run(_run())


def test_heartbeat_renews_live_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:
            started = asyncio.Event()

            async def slow(conn: psycopg.AsyncConnection, job: Job) -> str:
                started.set()
                await asyncio.sleep(2.0)  # outlives the 1 s lease
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, slow)
            worker = Worker(
                pool,
                reg,
                worker_id="w1",
                lease=timedelta(seconds=1),
                heartbeat_interval=timedelta(milliseconds=250),
            )
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb-live")

            task = asyncio.create_task(worker.run_once())
            await started.wait()

            await asyncio.sleep(0.5)
            async with pool.connection() as c:
                cur = await c.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (job.id,))
                r1 = await cur.fetchone()
            await asyncio.sleep(0.6)
            async with pool.connection() as c:
                cur = await c.execute(
                    "SELECT lease_expires_at, worker_id FROM jobs WHERE id = %s", (job.id,)
                )
                r2 = await cur.fetchone()

            assert r1 is not None and r2 is not None
            assert r2[0] > r1[0]  # the heartbeat advanced the lease mid-run
            assert r2[1] == "w1"  # never reclaimed
            await task
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED

    asyncio.run(_run())


def test_heartbeat_error_does_not_crash_dispatch(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:

            async def boom(*args: object, **kwargs: object) -> bool:
                raise RuntimeError("heartbeat db error")

            monkeypatch.setattr(queue, "heartbeat", boom)

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                await asyncio.sleep(0.5)  # long enough for a heartbeat to fire and fail
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(
                pool,
                reg,
                worker_id="w1",
                lease=timedelta(seconds=1),
                heartbeat_interval=timedelta(milliseconds=100),
            )
            async with pool.connection() as conn:
                job = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hberr")

            processed = await worker.run_once()  # a failing heartbeat must not raise here
            assert processed is not None
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED  # finalized despite the bad heartbeat

    asyncio.run(_run())


def test_run_survives_run_once_error(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            worker = Worker(
                pool, HandlerRegistry(), worker_id="w1", poll_interval=timedelta(milliseconds=10)
            )
            stop = asyncio.Event()
            calls = 0

            async def fake_run_once() -> Job | None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("transient dequeue error")
                stop.set()
                return None

            monkeypatch.setattr(worker, "run_once", fake_run_once)
            await asyncio.wait_for(worker.run(stop), timeout=2)
            assert calls >= 2  # the loop survived the first iteration's error and ran again

    asyncio.run(_run())
