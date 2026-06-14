"""Tests for the connection-scoped queue operations (ADR-0018)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.build_hosts import WORKER_LOCAL_ID
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, BuildPayload, SystemPayload

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="a")


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()), build_host_id=str(WORKER_LOCAL_ID))


def _system_payload() -> SystemPayload:
    return SystemPayload(system_id=str(uuid4()))


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
            payload = _build_payload()
            authorizing = Authorizing(principal="alice", agent_session=None, project="kernel-team")
            job = await queue.enqueue(conn, JobKind.BUILD, payload, authorizing, "dk-1")
            assert isinstance(job, Job)
            assert job.state is JobState.QUEUED
            assert job.attempt == 0
            assert job.payload == payload.model_dump(mode="json", exclude_none=True)
            assert job.authorizing == authorizing.model_dump(mode="json")
            assert job.dedup_key == "dk-1"
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_same_dedup_key_returns_same_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await queue.enqueue(
                conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-dup"
            )
            second = await queue.enqueue(
                conn,
                JobKind.PROVISION,
                _system_payload(),
                Authorizing(principal="p", project="b"),
                "dk-dup",
            )
            assert second.id == first.id
            assert second.kind is JobKind.BUILD  # the existing row, unchanged
            assert await _count_jobs(conn) == 1

    asyncio.run(_run())


def test_enqueue_distinct_dedup_keys_make_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-a")
            b = await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-b")
            assert a.id != b.id
            assert await _count_jobs(conn) == 2

    asyncio.run(_run())


def test_enqueue_rejects_max_attempts_below_one(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ValueError, match="max_attempts"):
                await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-0", max_attempts=0
                )

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
            "    %s, %s) RETURNING *",
            (
                attempt,
                max_attempts,
                worker_id,
                lease_seconds,
                Jsonb(_AUTHORIZING.model_dump(mode="json")),
                dedup_key,
            ),
        )
        row = await cur.fetchone()
    return Job.model_validate(row)


def test_dequeue_claims_oldest_and_charges_attempt(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            old = await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-old")
            new = await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-new")
            await conn.execute(
                "UPDATE jobs SET created_at = CASE "
                "WHEN id = %s THEN timestamp '2026-01-01 00:00:00+00' "
                "WHEN id = %s THEN timestamp '2026-01-01 00:00:01+00' "
                "END WHERE id IN (%s, %s)",
                (old.id, new.id, old.id, new.id),
            )
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
            await queue.enqueue(setup, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-1")
            await queue.enqueue(setup, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-2")
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
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-hb")
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
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-hb2")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            assert await queue.heartbeat(conn, claimed.id, "intruder") is False

    asyncio.run(_run())


def test_complete_for_owner_and_none_for_non_owner(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-c1")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            done = await queue.complete(conn, claimed.id, "w1", "s3://result")
            assert done is not None
            assert done.state is JobState.SUCCEEDED
            assert done.result_ref == "s3://result"

            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-c2")
            other = await queue.dequeue(conn, "w1")
            assert other is not None
            assert await queue.complete(conn, other.id, "intruder", "s3://x") is None

    asyncio.run(_run())


def test_fail_requeues_below_max(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(
                conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-f1", max_attempts=3
            )
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
            await queue.enqueue(
                conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-f3", max_attempts=3
            )
            claimed = await queue.dequeue(conn, "w1")  # attempt -> 1, below max
            assert claimed is not None
            out = await queue.fail(conn, claimed, ErrorCategory.NOT_IMPLEMENTED, terminal=True)
            assert out.state is JobState.FAILED
            assert out.error_category is ErrorCategory.NOT_IMPLEMENTED

    asyncio.run(_run())


def test_fail_fence_miss_returns_input(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-f4")
            claimed = await queue.dequeue(conn, "w1")
            assert claimed is not None
            # Simulate a reclaim by another worker: change worker_id out from under it.
            await conn.execute("UPDATE jobs SET worker_id = 'w2' WHERE id = %s", (claimed.id,))
            out = await queue.fail(conn, claimed, ErrorCategory.INFRASTRUCTURE_FAILURE)
            assert out is claimed  # fence missed: unchanged input returned

    asyncio.run(_run())


def test_recent_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            for i in range(3):
                await queue.enqueue(
                    conn,
                    JobKind.BUILD,
                    _build_payload(),
                    cast(Any, {"principal": "p", "project": "proj"}),
                    f"d{i}",
                )
            recent = await queue.recent_jobs(conn, limit=2, projects=["proj"])
        assert len(recent) == 2
        # newest-first: the last-enqueued dedup_key appears first
        assert recent[0].dedup_key == "d2"
        assert recent[1].dedup_key == "d1"

    asyncio.run(_run())


def test_recent_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.recent_jobs(conn, limit=10, projects=["proj"]) == []

    asyncio.run(_run())


def test_recent_jobs_filters_by_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "ja")
            await queue.enqueue(
                conn,
                JobKind.BUILD,
                _build_payload(),
                Authorizing(principal="p", project="b"),
                "jb",
            )
            await conn.execute(
                "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                "VALUES ('build', %s, 'queued', 3, %s, 'jnone')",
                (
                    Jsonb(_build_payload().model_dump(mode="json", exclude_none=True)),
                    Jsonb({"principal": "p"}),
                ),
            )
            recent = await queue.recent_jobs(conn, limit=10, projects=["a"])
        assert [j.dedup_key for j in recent] == ["ja"]  # only project a; b and no-project excluded

    asyncio.run(_run())


def test_recent_jobs_empty_projects_returns_nothing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await queue.enqueue(conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "ja")
            assert await queue.recent_jobs(conn, limit=10, projects=[]) == []

    asyncio.run(_run())


def test_queue_paused_defaults_false_and_toggles(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            assert await queue.is_queue_paused(conn) is False  # seeded row, default false
            await queue.set_queue_paused(conn, True)
            assert await queue.is_queue_paused(conn) is True
            await queue.set_queue_paused(conn, False)
            assert await queue.is_queue_paused(conn) is False

    asyncio.run(_run())


def test_ops_control_is_single_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            cur = await conn.execute("SELECT count(*) FROM ops_control")
            row = await cur.fetchone()
            assert row is not None and row[0] == 1  # seeded exactly once
            with pytest.raises(psycopg.errors.UniqueViolation):
                await conn.execute("INSERT INTO ops_control (singleton) VALUES (true)")

    asyncio.run(_run())


def test_is_queue_paused_fails_closed_when_row_missing(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await conn.execute("DELETE FROM ops_control")
            assert await queue.is_queue_paused(conn) is True  # missing row → fail closed (paused)

    asyncio.run(_run())
