"""Tests for the worker claim/dispatch loop (ADR-0018)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.errors import ErrorCategory
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
