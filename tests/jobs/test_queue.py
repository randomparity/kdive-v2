"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg.rows import dict_row

from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _count_jobs(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM jobs")
    row = await cur.fetchone()
    assert row is not None  # COUNT(*) always returns one row
    return row[0]


def test_enqueue_inserts_queued_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            job = await queue.enqueue(conn, JobKind.BUILD, {"x": 1}, {"principal": "alice"}, "dk-1")
            assert isinstance(job, Job)
            assert job.state is JobState.QUEUED
            assert job.attempt == 0
            assert job.payload == {"x": 1}
            assert job.authorizing == {"principal": "alice"}
            assert job.dedup_key == "dk-1"
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_same_dedup_key_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-dup")
            second = await queue.enqueue(conn, JobKind.PROVISION, {"y": 2}, {"p": "b"}, "dk-dup")
            assert second.id == first.id
            assert second.kind is JobKind.BUILD  # the existing row, unchanged
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_distinct_dedup_keys_make_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-a")
            b = await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-b")
            assert a.id != b.id
            assert await _count_jobs(conn) == 2

    asyncio.run(_run())


def test_enqueue_rejects_max_attempts_below_one(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="max_attempts"):
                await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-0", max_attempts=0)

    asyncio.run(_run())


async def _insert_running_job(
    conn: psycopg.AsyncConnection,
    dedup_key: str,
    *,
    worker_id: str = "dead",
    lease_seconds: int,
    attempt: int = 0,
    max_attempts: int = 3,
) -> Job:
    """Insert a job already in ``running`` with a lease ``lease_seconds`` from now.

    Negative ``lease_seconds`` makes the lease already lapsed. The timestamp is
    computed in SQL (``now() + make_interval(...)``) — a relative interval cannot be
    passed as a bound parameter to a ``timestamptz`` column.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
            "    lease_expires_at, authorizing, dedup_key) "
            "VALUES ('build', '{}', 'running', %s, %s, %s, now() + make_interval(secs => %s), "
            "    '{}', %s) RETURNING *",
            (attempt, max_attempts, worker_id, lease_seconds, dedup_key),
        )
        row = await cur.fetchone()
    return Job.model_validate(row)


def test_dequeue_claims_oldest_and_charges_attempt(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-old")
            await asyncio.sleep(0.01)
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-new")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            assert claimed.dedup_key == "dk-old"  # FIFO by created_at
            assert claimed.state is JobState.RUNNING
            assert claimed.worker_id == "w1"
            assert claimed.attempt == 1
            assert claimed.lease_expires_at is not None

    asyncio.run(_run())


def test_dequeue_empty_returns_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.dequeue(conn, "w1") is None

    asyncio.run(_run())


def test_dequeue_concurrent_claims_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as setup:
            await queue.enqueue(setup, JobKind.BUILD, {}, {"p": "a"}, "dk-1")
            await queue.enqueue(setup, JobKind.BUILD, {}, {"p": "a"}, "dk-2")
        async with await _connect(migrated_url) as a, await _connect(migrated_url) as b:
            ja, jb = await asyncio.gather(queue.dequeue(a, "wa"), queue.dequeue(b, "wb"))
        assert ja is not None and jb is not None
        assert ja.id != jb.id  # SKIP LOCKED: no double-claim

    asyncio.run(_run())


def test_dequeue_skips_future_lease_reclaims_past_lease(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-future", lease_seconds=300)
            assert await queue.dequeue(conn, "w1") is None  # live lease: not reclaimed

            await _insert_running_job(conn, "dk-past", lease_seconds=-60)
            reclaimed = await queue.dequeue(conn, "w1")
            assert reclaimed is not None
            assert reclaimed.dedup_key == "dk-past"
            assert reclaimed.worker_id == "w1"
            assert reclaimed.attempt == 1  # 0 -> 1 on reclaim

    asyncio.run(_run())


def test_dequeue_skips_exhausted_attempts(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _insert_running_job(conn, "dk-done", lease_seconds=-60, attempt=3, max_attempts=3)
            assert await queue.dequeue(conn, "w1") is None  # attempt == max: left for reconciler

    asyncio.run(_run())
