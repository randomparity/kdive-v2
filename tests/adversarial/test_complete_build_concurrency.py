"""Concurrent complete_build serializes to one ledger row (ADR-0048 §6).

Two simultaneous complete_build calls on the same Run must collapse to exactly
one run_steps 'build' row and one created → succeeded transition.  The per-Run
advisory lock + ON CONFLICT DO NOTHING + WHERE state='created' UPDATE fence
provide the guarantee; this test proves it against a live Postgres instance.

Validation happens before the lock is acquired, so both racers may call the
validator (calls==1 or calls==2 are both acceptable).  Only one racer finalizes.
"""

from __future__ import annotations

import asyncio

from kdive.db.repositories import RUNS
from kdive.domain.state import RunState
from kdive.mcp.tools.lifecycle import runs as runs_tools
from kdive.providers.ports import BuildOutput
from tests.mcp.complete_build_support import (
    FakeValidator,
    ctx,
    pool,
    seed_external_run_with_manifest,
)


class _CountingValidator:
    """Wraps the Task 8 fake validator; tracks total validate() invocations."""

    def __init__(self, output: BuildOutput) -> None:
        self._inner = FakeValidator(output)
        self.calls = 0

    def validate(self, run_id, manifest, keys, declared_build_id):
        self.calls += 1
        return self._inner.validate(run_id, manifest, keys, declared_build_id)


def test_concurrent_complete_build_yields_one_ledger_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with pool(migrated_url) as conn_pool:
            run_id = await seed_external_run_with_manifest(conn_pool)
            validator = _CountingValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            results = await asyncio.gather(
                runs_tools.complete_build(
                    conn_pool, ctx(), str(run_id), build_id=None, cmdline="c", validator=validator
                ),
                runs_tools.complete_build(
                    conn_pool, ctx(), str(run_id), build_id=None, cmdline="c", validator=validator
                ),
            )
            assert all(r.status == "succeeded" for r in results), (
                f"Expected both results to succeed, got: {[r.status for r in results]}"
            )
            assert validator.calls in (1, 2), (
                "validator must run at least once and at most once per racer: "
                f"both may validate before the lock, or the second may hit the "
                f"idempotent short-read, but got {validator.calls} calls"
            )
            async with conn_pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM run_steps WHERE run_id = %s AND step = 'build'",
                    (run_id,),
                )
                row = await cur.fetchone()
                assert row is not None and row[0] == 1, (
                    f"Expected exactly 1 build ledger row, got: {row[0] if row else None}"
                )
                run = await RUNS.get(conn, run_id)
            assert run is not None and run.state is RunState.SUCCEEDED, (
                f"Expected run state SUCCEEDED, got: {run.state if run else None}"
            )

    asyncio.run(_run())
