"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from kdive.domain.errors import ErrorCategory
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


def test_heartbeat_renews_for_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb")
            claimed = await queue.dequeue(conn, "w1", lease=timedelta(seconds=10))
            assert claimed is not None
            assert claimed.lease_expires_at is not None
            assert await queue.heartbeat(conn, claimed.id, "w1", lease=timedelta(minutes=5)) is True
            cur = await conn.execute(
                "SELECT lease_expires_at FROM jobs WHERE id = %s", (claimed.id,)
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] > claimed.lease_expires_at

    asyncio.run(_run())


def test_heartbeat_false_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-hb2")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            assert await queue.heartbeat(conn, claimed.id, "intruder") is False

    asyncio.run(_run())


def test_complete_for_owner_and_none_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-c1")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(conn, claimed.id, "w1", "s3://result")
            assert done is not None
            assert done.state is JobState.SUCCEEDED
            assert done.result_ref == "s3://result"

            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-c2")
            other = await queue.dequeue(conn, "w1")
            assert other is not None
            assert await queue.complete(conn, other.id, "intruder", "s3://x") is None

    asyncio.run(_run())


def test_fail_requeues_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f1", max_attempts=3)
            claimed = await queue.dequeue(conn, "w1")  # attempt -> 1
            assert claimed is not None
            out = await queue.fail(conn, claimed, ErrorCategory.INFRASTRUCTURE_FAILURE)
            assert out.state is JobState.QUEUED
            assert out.worker_id is None
            assert out.lease_expires_at is None

    asyncio.run(_run())


def test_fail_dead_letters_at_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            claimed = await _insert_running_job(
                conn, "dk-f2", worker_id="w1", lease_seconds=300, attempt=3, max_attempts=3
            )
            out = await queue.fail(conn, claimed, ErrorCategory.BUILD_FAILURE)
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.BUILD_FAILURE

    asyncio.run(_run())


def test_fail_terminal_dead_letters_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f3", max_attempts=3)
            claimed = await queue.dequeue(conn, "w1")  # attempt -> 1, below max
            assert claimed is not None
            out = await queue.fail(conn, claimed, ErrorCategory.NOT_IMPLEMENTED, terminal=True)
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.NOT_IMPLEMENTED

    asyncio.run(_run())


def test_fail_fence_miss_returns_input(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, {}, {"p": "a"}, "dk-f4")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            # Simulate a reclaim by another worker: change worker_id out from under it.
            await conn.execute("UPDATE jobs SET worker_id = 'w2' WHERE id = %s", (claimed.id,))
            out = await queue.fail(conn, claimed, ErrorCategory.INFRASTRUCTURE_FAILURE)
            assert out is claimed  # fence missed: unchanged input returned

    asyncio.run(_run())
