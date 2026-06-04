"""Adversarial: the boundary of `run_step`'s idempotency guarantee.

`db/idempotency.py`'s module docstring says `run_step` "runs a step's function at
most once per (run_id, step)". Under genuinely concurrent callers on distinct
connections at READ COMMITTED that claim is **false for the side effect**: both
callers `SELECT`-miss before either `INSERT` commits, so both run `fn`. Only the
*result* is de-duplicated (the ledger row). The sole production caller
(`mcp/tools/runs.py::_run_step_locked`) already knows this and serializes the
whole `run_step` under `LockScope.RUN`; these tests pin both facts so the weaker
bare-function contract is documented and the lock's necessity is regression-proof.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from kdive.db.idempotency import JsonValue, run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from tests.adversarial.conftest import open_conn, open_conns, seed_run


def _counting_fn(state: dict[str, int], barrier: asyncio.Barrier | None, delay: float):
    """An fn that counts its invocations, optionally rendezvousing on ``barrier``."""

    async def fn() -> JsonValue:
        state["calls"] += 1
        if barrier is not None:
            await barrier.wait()
        await asyncio.sleep(delay)
        return {"n": state["calls"]}

    return fn


def test_bare_run_step_double_executes_fn_under_concurrency(migrated_url: str) -> None:
    # The falsifying case: a Barrier forces both callers past their SELECT-miss before
    # either INSERT, so fn provably runs twice while the *result* stays de-duplicated.
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            run_id: UUID = await seed_run(seed)
        state = {"calls": 0}
        barrier = asyncio.Barrier(2)
        async with open_conns(migrated_url, 2) as (c1, c2):
            results = await asyncio.gather(
                run_step(c1, run_id, "step", _counting_fn(state, barrier, 0.05)),
                run_step(c2, run_id, "step", _counting_fn(state, barrier, 0.05)),
            )
        assert state["calls"] == 2, "bare run_step did NOT double-run fn — claim re-examine"
        assert results[0] == results[1], "ledger row must still de-dup the stored result"

    asyncio.run(_run())


def test_run_step_under_run_lock_executes_fn_exactly_once(migrated_url: str) -> None:
    # The production contract: holding LockScope.RUN around run_step serializes the
    # callers, so the second sees the committed ledger row and skips fn entirely.
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            run_id = await seed_run(seed)
        state = {"calls": 0}

        async def locked_step(conn) -> JsonValue:
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
                return await run_step(conn, run_id, "step", _counting_fn(state, None, 0.05))

        async with open_conns(migrated_url, 2) as (c1, c2):
            results = await asyncio.gather(locked_step(c1), locked_step(c2))
        assert state["calls"] == 1, "the RUN lock must make fn run exactly once"
        assert results[0] == results[1] == {"n": 1}

    asyncio.run(_run())


def test_run_step_replay_returns_jsonb_roundtripped_result(migrated_url: str) -> None:
    # A recorded step replays the value as read back from jsonb, identical across calls.
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            run_id = await seed_run(seed)
        async with open_conn(migrated_url) as conn:
            first = await run_step(conn, run_id, "s", lambda: _ok())
            second = await run_step(conn, run_id, "s", lambda: _boom())
            assert first == second  # replay ignores the second fn entirely

    asyncio.run(_run())


async def _ok() -> JsonValue:
    return {"ok": True, "items": [1, 2, 3]}


async def _boom() -> JsonValue:
    raise AssertionError("fn must not run on replay")
