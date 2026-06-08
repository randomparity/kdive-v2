"""Tests for the worker claim/dispatch loop (ADR-0018)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import BuildPayload
from kdive.jobs.worker import Worker
from kdive.security.secrets.secret_registry import SecretRegistry

_AUTHORIZING = {"principal": "p", "agent_session": None, "project": "a"}


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()))


async def _final_state(url: str, job_id: UUID) -> Job:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        job = await JOBS.get(conn, job_id)
    assert job is not None
    return job


def _unopened_pool(max_size: int = 4) -> AsyncConnectionPool:
    """A type-correct pool that never connects — the construct guard runs before use."""
    return AsyncConnectionPool(
        "postgresql://localhost/unused", min_size=1, max_size=max_size, open=False
    )


def test_init_rejects_pool_too_small_for_dispatch_plus_heartbeat() -> None:
    with pytest.raises(ValueError, match="max_size"):
        Worker(_unopened_pool(max_size=1), HandlerRegistry(), worker_id="w1")


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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-happy"
                )

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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-unk"
                )
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
                first = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-dedup"
                )
                second = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-dedup"
                )
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
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-poison", max_attempts=3
                )

            for _ in range(3):
                await worker.run_once()
            assert calls == 3
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.error_category is ErrorCategory.BUILD_FAILURE
            assert await worker.run_once() is None  # dead-lettered: not re-dequeued

    asyncio.run(_run())


def test_failed_job_persists_redacted_failure_context(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:
            run_id = uuid4()
            secret_registry = SecretRegistry()
            secret_registry.register("supersecret", scope=None)

            async def always_raises(conn: psycopg.AsyncConnection, job: Job) -> str:
                raise CategorizedError(
                    "saw supersecret build failed",
                    category=ErrorCategory.BUILD_FAILURE,
                    details={"run_id": run_id, "payload": {"not": "safe"}},
                )

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, always_raises)
            worker = Worker(pool, reg, worker_id="w1", secret_registry=secret_registry)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-context"
                )

            for _ in range(queue.DEFAULT_MAX_ATTEMPTS):
                await worker.run_once()
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.FAILED
            assert final.failure_context == {
                "failure_message": "saw [REDACTED] build failed",
                "failure_detail_run_id": str(run_id),
            }
            records = [record for record in caplog.records if "failed:" in record.getMessage()]
            assert records and records[0].exc_info is not None

    caplog.set_level(logging.WARNING, logger="kdive.jobs.worker")
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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-lapse"
                )
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


def test_heartbeat_renews_live_lease(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:
            started = asyncio.Event()
            first_heartbeat = asyncio.Event()
            second_heartbeat = asyncio.Event()
            heartbeat_count = 0

            original_heartbeat = queue.heartbeat

            async def observed_heartbeat(
                conn: psycopg.AsyncConnection,
                job_id: UUID,
                worker_id: str,
                *,
                lease: timedelta = queue.DEFAULT_LEASE,
            ) -> bool:
                nonlocal heartbeat_count
                ok = await original_heartbeat(conn, job_id, worker_id, lease=lease)
                heartbeat_count += 1
                if heartbeat_count == 1:
                    first_heartbeat.set()
                elif heartbeat_count == 2:
                    second_heartbeat.set()
                return ok

            monkeypatch.setattr(queue, "heartbeat", observed_heartbeat)

            async def slow(conn: psycopg.AsyncConnection, job: Job) -> str:
                started.set()
                await asyncio.wait_for(second_heartbeat.wait(), timeout=5)
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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-hb-live"
                )

            task = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(started.wait(), timeout=5)

            await asyncio.wait_for(first_heartbeat.wait(), timeout=5)
            async with pool.connection() as c:
                cur = await c.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (job.id,))
                r1 = await cur.fetchone()
            await asyncio.wait_for(second_heartbeat.wait(), timeout=5)
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
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:
            heartbeat_attempted = asyncio.Event()

            async def boom(*args: object, **kwargs: object) -> bool:
                heartbeat_attempted.set()
                raise RuntimeError("heartbeat db error")

            monkeypatch.setattr(queue, "heartbeat", boom)

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                await asyncio.wait_for(heartbeat_attempted.wait(), timeout=5)
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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-hberr"
                )

            processed = await worker.run_once()  # a failing heartbeat must not raise here
            assert processed is not None
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED  # finalized despite the bad heartbeat
            records = [
                record for record in caplog.records if "heartbeat for job" in record.getMessage()
            ]
            assert records and records[0].exc_info is not None

    caplog.set_level(logging.WARNING, logger="kdive.jobs.worker")
    asyncio.run(_run())


def test_run_once_claims_nothing_while_paused(migrated_url: str) -> None:
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
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-paused"
                )
                await queue.set_queue_paused(conn, True)

            assert await worker.run_once() is None  # paused: no claim
            assert calls == 0
            still_queued = await _final_state(migrated_url, job.id)
            assert still_queued.state is JobState.QUEUED
            assert still_queued.attempt == 0  # never charged a claim while paused

    asyncio.run(_run())


def test_resume_restores_claiming(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=10) as pool:

            async def handler(conn: psycopg.AsyncConnection, job: Job) -> str:
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, handler)
            worker = Worker(pool, reg, worker_id="w1")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-resume"
                )
                await queue.set_queue_paused(conn, True)
            assert await worker.run_once() is None  # paused

            async with pool.connection() as conn:
                await queue.set_queue_paused(conn, False)
            processed = await worker.run_once()  # resume restores claiming
            assert processed is not None and processed.id == job.id
            final = await _final_state(migrated_url, job.id)
            assert final.state is JobState.SUCCEEDED

    asyncio.run(_run())


def test_paused_worker_completes_job_already_in_flight(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=3, max_size=10) as pool:
            started = asyncio.Event()
            may_finish = asyncio.Event()

            async def slow(conn: psycopg.AsyncConnection, job: Job) -> str:
                started.set()
                await asyncio.wait_for(may_finish.wait(), timeout=5)
                return "s3://out"

            reg = HandlerRegistry()
            reg.register(JobKind.BUILD, slow)
            worker = Worker(
                pool,
                reg,
                worker_id="w1",
                lease=timedelta(seconds=2),
                heartbeat_interval=timedelta(milliseconds=200),
            )
            async with pool.connection() as conn:
                in_flight = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-inflight"
                )
                later = await queue.enqueue(
                    conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-later"
                )

            task = asyncio.create_task(worker.run_once())  # claims in_flight, blocks in handler
            await asyncio.wait_for(started.wait(), timeout=5)

            # Pause mid-flight, then let the in-flight handler finish.
            async with pool.connection() as conn:
                await queue.set_queue_paused(conn, True)
            may_finish.set()
            processed = await task
            assert processed is not None and processed.id == in_flight.id
            completed = await _final_state(migrated_url, in_flight.id)
            assert completed.state is JobState.SUCCEEDED  # in-flight job completed despite pause

            # The second, still-queued job is not claimed while paused.
            assert await worker.run_once() is None
            assert (await _final_state(migrated_url, later.id)).state is JobState.QUEUED

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


def test_run_stops_while_idle_sleep_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        worker = Worker(
            _unopened_pool(),
            HandlerRegistry(),
            worker_id="w1",
            poll_interval=timedelta(seconds=30),
        )
        stop = asyncio.Event()
        idle_reached = asyncio.Event()

        async def fake_run_once() -> Job | None:
            idle_reached.set()
            return None

        monkeypatch.setattr(worker, "run_once", fake_run_once)
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(idle_reached.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())


def test_run_stops_while_error_sleep_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        worker = Worker(
            _unopened_pool(),
            HandlerRegistry(),
            worker_id="w1",
            poll_interval=timedelta(seconds=30),
        )
        stop = asyncio.Event()
        error_reached = asyncio.Event()

        async def fake_run_once() -> Job | None:
            error_reached.set()
            raise RuntimeError("transient dequeue error")

        monkeypatch.setattr(worker, "run_once", fake_run_once)
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(error_reached.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())
