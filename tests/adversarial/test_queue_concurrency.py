"""Adversarial: the durable job queue under concurrent workers.

Invariants (ADR-0018, `jobs/queue.py`):
  * mutual exclusion — `FOR UPDATE SKIP LOCKED` gives each queued job to exactly
    one concurrent `dequeue`, never two;
  * bounded execution — charging an attempt on every claim caps total claims of a
    job at `max_attempts`, even across worker death (lease lapse → reclaim);
  * the post-claim fence (`worker_id` + `state='running'`) stops a worker that
    lost its lease from finalizing a job another worker now owns;
  * `enqueue` is idempotent on `dedup_key` even when calls race on distinct
    connections.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from kdive.db.build_hosts import WORKER_LOCAL_ID
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import JobKind
from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, BuildPayload
from tests.adversarial.conftest import count_rows, open_conn, open_conns

_AUTHORIZING = Authorizing(principal="p", agent_session=None, project="a")


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()), build_host_id=str(WORKER_LOCAL_ID))


async def _expire_lease(conn: psycopg.AsyncConnection, job_id: object) -> None:
    """Force ``job_id``'s lease into the past so the next dequeue may reclaim it."""
    await conn.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = %s",
        (job_id,),
    )


@pytest.mark.parametrize(("jobs", "workers"), [(5, 12), (10, 10), (20, 4)])
def test_concurrent_dequeue_claims_each_job_once(
    migrated_url: str, jobs: int, workers: int
) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            for i in range(jobs):
                await queue.enqueue(seed, JobKind.BUILD, _build_payload(), _AUTHORIZING, f"dk-{i}")
        async with open_conns(migrated_url, workers) as conns:
            claimed = await asyncio.gather(
                *(queue.dequeue(c, f"w{i}") for i, c in enumerate(conns))
            )
        won = [j for j in claimed if j is not None]
        ids = [j.id for j in won]
        assert len(ids) == min(jobs, workers)
        assert len(set(ids)) == len(ids), "a job was claimed by more than one worker"
        assert all(j.state is JobState.RUNNING and j.attempt == 1 for j in won)

    asyncio.run(_run())


def test_attempt_charging_caps_total_claims_across_reclaim(migrated_url: str) -> None:
    # A job claimed, abandoned (lease lapses), reclaimed — repeated — must become
    # unclaimable after exactly max_attempts claims, never an unbounded redelivery.
    max_attempts = 3

    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            job = await queue.enqueue(
                conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk", max_attempts=max_attempts
            )
            claims = 0
            for _ in range(max_attempts + 5):  # try well past the cap
                got = await queue.dequeue(conn, "w")
                if got is None:
                    break
                claims += 1
                await _expire_lease(conn, job.id)
            assert claims == max_attempts, f"claimed {claims} times, cap is {max_attempts}"
            assert await queue.dequeue(conn, "w") is None

    asyncio.run(_run())


def test_reclaimed_worker_cannot_finalize(migrated_url: str) -> None:
    # Worker A claims, loses its lease, worker B reclaims. A's complete/heartbeat/fail
    # must all no-op against B's running job; only B may finalize it.
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            job = await queue.enqueue(
                conn, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk", max_attempts=5
            )
            claimed_a = await queue.dequeue(conn, "A")
            assert claimed_a is not None and claimed_a.worker_id == "A"
            await _expire_lease(conn, job.id)
            claimed_b = await queue.dequeue(conn, "B")
            assert claimed_b is not None and claimed_b.worker_id == "B"

            # A lost the lease: every A-fenced write must miss. complete/heartbeat
            # signal the miss directly; fail() signals it by returning the *unchanged*
            # input job (worker_id still 'A') rather than a post-write row.
            assert await queue.heartbeat(conn, job.id, "A") is False
            assert await queue.complete(conn, job.id, "A", "ref-from-A") is None
            failed_by_a = await queue.fail(conn, claimed_a, ErrorCategory.BUILD_FAILURE)
            assert failed_by_a is claimed_a, "fail() returns the input unchanged on a fence miss"

            # The row itself is untouched by A: still B's running job.
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT state, worker_id, result_ref FROM jobs WHERE id = %s", (job.id,)
                )
                row = await cur.fetchone()
            assert row == ("running", "B", None), f"A mutated B's job: {row}"

            # B still owns it and can finalize.
            done = await queue.complete(conn, job.id, "B", "ref-from-B")
            assert done is not None and done.state is JobState.SUCCEEDED
            assert done.result_ref == "ref-from-B"

    asyncio.run(_run())


@pytest.mark.parametrize("racers", [4, 12])
def test_concurrent_enqueue_same_dedup_key_makes_one_row(migrated_url: str, racers: int) -> None:
    async def _run() -> None:
        async with open_conns(migrated_url, racers) as conns:
            jobs = await asyncio.gather(
                *(
                    queue.enqueue(c, JobKind.BUILD, _build_payload(), _AUTHORIZING, "dk-shared")
                    for i, c in enumerate(conns)
                )
            )
        ids = {j.id for j in jobs}
        assert len(ids) == 1, f"dedup_key race created {len(ids)} distinct jobs"
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "jobs") == 1

    asyncio.run(_run())
