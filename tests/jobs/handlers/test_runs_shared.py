"""Direct tests for shared run worker helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import WORKER_LOCAL_ID
from kdive.db.repositories import RUNS
from kdive.domain.models import JobKind
from kdive.domain.state import RunState, SystemState
from kdive.jobs import queue
from kdive.jobs.handlers.runs_shared import finalize_build
from kdive.jobs.payloads import BuildPayload
from kdive.services.runs.steps import BuildStepResult
from tests.integration._seed import (
    seed_granted_allocation,
    seed_running_run,
    seed_system,
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_run(pool: AsyncConnectionPool) -> str:
    allocation_id = await seed_granted_allocation(pool)
    system_id = await seed_system(pool, allocation_id, SystemState.READY)
    return await seed_running_run(pool, system_id)


async def _job(pool: AsyncConnectionPool, run_id: str):
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.BUILD,
            BuildPayload(run_id=run_id, build_host_id=str(WORKER_LOCAL_ID)),
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{run_id}:build",
        )


def _result(run_id: str) -> BuildStepResult:
    return BuildStepResult(
        kernel_ref=f"proj/runs/{run_id}/kernel",
        debuginfo_ref=f"proj/runs/{run_id}/vmlinux",
        build_id="abcdef0123456789",
        cmdline="dhash_entries=1",
    )


def test_finalize_build_records_step_updates_run_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _job(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                await finalize_build(conn, job, run, _result(run_id))
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT state, kernel_ref, debuginfo_ref FROM runs WHERE id = %s",
                        (run_id,),
                    )
                    run_row = await cur.fetchone()
                    await cur.execute(
                        "SELECT state, result FROM run_steps WHERE run_id = %s AND step = 'build'",
                        (run_id,),
                    )
                    step_row = await cur.fetchone()
                    await cur.execute(
                        "SELECT count(*) AS n FROM audit_log "
                        "WHERE object_id = %s AND transition = 'running->succeeded'",
                        (run_id,),
                    )
                    audit_row = await cur.fetchone()
            assert run_row is not None
            assert run_row["state"] == "succeeded"
            assert run_row["kernel_ref"] == f"proj/runs/{run_id}/kernel"
            assert run_row["debuginfo_ref"] == f"proj/runs/{run_id}/vmlinux"
            assert step_row is not None
            assert step_row["state"] == "succeeded"
            assert step_row["result"]["cmdline"] == "dhash_entries=1"
            assert audit_row is not None and audit_row["n"] == 1

    asyncio.run(_run())


def test_finalize_build_does_not_mutate_non_running_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            job = await _job(pool, run_id)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET state = %s, failure_category = 'build_failure' WHERE id = %s",
                    (RunState.FAILED.value, run_id),
                )
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                await finalize_build(conn, job, run, _result(run_id))
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT state, kernel_ref, debuginfo_ref FROM runs WHERE id = %s",
                        (run_id,),
                    )
                    run_row = await cur.fetchone()
                    await cur.execute(
                        "SELECT count(*) AS n FROM run_steps WHERE run_id = %s AND step = 'build'",
                        (run_id,),
                    )
                    step_row = await cur.fetchone()
                    await cur.execute(
                        "SELECT count(*) AS n FROM audit_log "
                        "WHERE object_id = %s AND transition = 'running->succeeded'",
                        (run_id,),
                    )
                    audit_row = await cur.fetchone()
            assert run_row is not None
            assert run_row["state"] == "failed"
            assert run_row["kernel_ref"] is None
            assert run_row["debuginfo_ref"] is None
            assert step_row is not None and step_row["n"] == 1
            assert audit_row is not None and audit_row["n"] == 0

    asyncio.run(_run())
