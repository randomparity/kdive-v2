"""Adversarial: the reconciler's abandoned-job sweep compensates atomically.

Invariant (ADR-0021, `reconciler/loop.py::_repair_abandoned_jobs`): a zombie job
(`running`, lapsed lease, `attempt >= max_attempts`) is dead-lettered *and*, when its
payload names a non-terminal run, that run is failed — in one transaction, so a crash
cannot strand the run un-compensated. A terminal run is left untouched; a job a worker
finalized first (fence miss) is skipped.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from kdive.reconciler.loop import _repair_abandoned_jobs
from tests.adversarial.conftest import one, open_conn, seed_run

_AUTHORIZING = {"principal": "reconciler-test", "agent_session": None, "project": "test"}


async def _make_zombie(conn: psycopg.AsyncConnection, run_id: UUID | None) -> UUID:
    payload = "{}" if run_id is None else f'{{"run_id": "{run_id}"}}'
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "('build', %s::jsonb, 'running', 1, 1, 'dead-worker', "
            "now() - interval '1 hour', now() - interval '1 hour', %s, %s) "
            "RETURNING id",
            (payload, Jsonb(_AUTHORIZING), f"dk-{run_id}"),
        )
        return (await one(cur))[0]


async def _state(conn: psycopg.AsyncConnection, table: str, oid: UUID) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT state FROM {} WHERE id = %s").format(sql.Identifier(table)), (oid,)
        )
        return (await one(cur))[0]


def test_zombie_with_run_fails_both_job_and_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            run_id = await seed_run(conn)
            job_id = await _make_zombie(conn, run_id)
            swept = await _repair_abandoned_jobs(conn)
            assert swept == 1
            assert await _state(conn, "jobs", job_id) == "failed"
            assert await _state(conn, "runs", run_id) == "failed"
            async with conn.cursor() as cur:
                await cur.execute("SELECT error_category FROM jobs WHERE id = %s", (job_id,))
                assert (await one(cur))[0] == "lease_expired"
                await cur.execute("SELECT failure_category FROM runs WHERE id = %s", (run_id,))
                assert (await one(cur))[0] == "lease_expired"

    asyncio.run(_run())


def test_zombie_leaves_already_terminal_run_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            run_id = await seed_run(conn)
            await conn.execute("UPDATE runs SET state = 'succeeded' WHERE id = %s", (run_id,))
            job_id = await _make_zombie(conn, run_id)
            swept = await _repair_abandoned_jobs(conn)
            assert swept == 1
            assert await _state(conn, "jobs", job_id) == "failed"  # job still dead-lettered
            assert await _state(conn, "runs", run_id) == "succeeded"  # run NOT clobbered

    asyncio.run(_run())


def test_zombie_without_run_id_only_fails_the_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            job_id = await _make_zombie(conn, None)
            swept = await _repair_abandoned_jobs(conn)
            assert swept == 1
            assert await _state(conn, "jobs", job_id) == "failed"

    asyncio.run(_run())


def test_healthy_running_job_is_not_swept(migrated_url: str) -> None:
    # A live lease (future expiry) must never be reaped by the abandoned-job sweep.
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
                    "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
                    "('build', '{}'::jsonb, 'running', 1, 3, 'live-worker', "
                    "now() + interval '5 minutes', now(), %s, 'dk-live') RETURNING id",
                    (Jsonb(_AUTHORIZING),),
                )
                job_id = (await one(cur))[0]
            assert await _repair_abandoned_jobs(conn) == 0
            assert await _state(conn, "jobs", job_id) == "running"

    asyncio.run(_run())
