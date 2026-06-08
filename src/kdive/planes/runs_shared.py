"""Worker-only run execution helpers."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.models import Job, Run
from kdive.domain.state import RunState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.security import audit
from kdive.services.run_steps import BuildStepResult


async def finalize_build(
    conn: AsyncConnection, job: Job, run: Run, result: BuildStepResult
) -> None:
    """Record the build ledger row and drive `running -> succeeded` under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result.dump())),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None or RunState(row["state"]) is not RunState.RUNNING:
            return
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'running'",
            (result.kernel_ref, result.debuginfo_ref, run.id),
        )
        await audit.record(
            conn,
            job_context_from_job(job, run.project),
            audit.AuditEvent(
                tool="runs.build",
                object_kind="runs",
                object_id=run.id,
                transition="running->succeeded",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
