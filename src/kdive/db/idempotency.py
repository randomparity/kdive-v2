"""Idempotent step execution backed by the `run_steps` ledger (ADR-0005, ADR-0016).

`run_step` de-dupes a step's **result** per `(run_id, step)`: a recorded row
short-circuits to the stored result; otherwise the function runs and its result is
stored under the unique `(run_id, step)` key. Every path returns the value as read
back from `jsonb`, so a replay equals the original even for a value the round-trip
would normalize. The concurrent-first-call resolution assumes the caller's
transaction runs at READ COMMITTED (psycopg's default).

**Scope of the guarantee — result-once, not fn-at-most-once.** Two callers racing on
distinct connections both `SELECT`-miss before either `INSERT` commits, so both run
`fn`; the unique `(run_id, step)` row then de-dupes the stored *result*, but `fn`'s
side effect has already happened twice. Bare `run_step` therefore guarantees a single
*stored result*, not a single *fn execution*. Use `claim_run_step` for provider side effects:
it commits a narrow `state='running'` ledger claim, lets the provider call run without a DB
transaction or advisory lock, and then records the successful JSON result with
`complete_run_step` (or removes the claim with `abandon_run_step` on failure). See
`tests/adversarial/test_idempotency_concurrency.py` for the bare `run_step` boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.serialization import JsonValue, ensure_json_value

_STEP_WAIT_POLL_SEC = 0.05
_STALE_RUNNING_INTERVAL = "30 minutes"


class StepClaim(NamedTuple):
    """A claimed run step, or a replayed result when the step already succeeded."""

    claimed: bool
    result: JsonValue


def _step_result(value: object, *, run_id: UUID, step: str) -> JsonValue:
    path = f"run_steps[{run_id!s}].{step}.result"
    return ensure_json_value(value, path=path)


async def run_step(
    conn: AsyncConnection,
    run_id: UUID,
    step: str,
    fn: Callable[[], Awaitable[JsonValue]],
) -> JsonValue:
    """Record ``step``'s result once for ``run_id``, returning it on replay.

    Guarantees a single *stored result*, not a single *fn execution*: concurrent
    callers on distinct connections can each run ``fn`` before either commits (see the
    module docstring). Serialize under an external per-scope lock if ``fn``'s side
    effect must not repeat.

    Args:
        conn: An async connection (READ COMMITTED).
        run_id: The owning run; must reference an existing ``runs`` row.
        step: The step name, unique within the run.
        fn: The step body, awaited when no result is recorded yet (possibly more than
            once across racing callers; only one result is stored).

    Returns:
        The step result as read back from ``jsonb`` (identical across replays).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step)
        )
        existing = await cur.fetchone()
        if existing is not None:
            return _step_result(existing["result"], run_id=run_id, step=step)
        result = _step_result(await fn(), run_id=run_id, step=step)
        await cur.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, %s, 'succeeded', %s) "
            "ON CONFLICT (run_id, step) DO NOTHING RETURNING result",
            (run_id, step, Jsonb(result)),
        )
        inserted = await cur.fetchone()
        if inserted is not None:
            return _step_result(inserted["result"], run_id=run_id, step=step)
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step)
        )
        winner = await cur.fetchone()
    if winner is None:  # Invariant: ON CONFLICT fired, so a committed row exists.
        raise RuntimeError(f"run_step ({run_id}, {step}) conflicted but found no row")
    return _step_result(winner["result"], run_id=run_id, step=step)


async def claim_run_step(conn: AsyncConnection, run_id: UUID, step: str) -> StepClaim:
    """Claim ``step`` for side-effect work without holding the DB lock while it runs.

    A caller that receives ``claimed=True`` owns the provider side effect and must later call
    ``complete_run_step`` or ``abandon_run_step``. Concurrent callers wait for a running claim
    to finish, then replay the stored result or claim the step if the owner abandoned it.
    """
    while True:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "DELETE FROM run_steps "
                "WHERE run_id = %s AND step = %s AND state = 'running' "
                "AND updated_at < now() - %s::interval",
                (run_id, step, _STALE_RUNNING_INTERVAL),
            )
            await cur.execute(
                "SELECT state, result FROM run_steps WHERE run_id = %s AND step = %s",
                (run_id, step),
            )
            existing = await cur.fetchone()
            if existing is None:
                await cur.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, %s, 'running', NULL) "
                    "ON CONFLICT (run_id, step) DO NOTHING RETURNING result",
                    (run_id, step),
                )
                inserted = await cur.fetchone()
                if inserted is not None:
                    return StepClaim(True, None)
                continue
            if existing["state"] == "succeeded":
                return StepClaim(False, _step_result(existing["result"], run_id=run_id, step=step))
        await asyncio.sleep(_STEP_WAIT_POLL_SEC)


async def complete_run_step(
    conn: AsyncConnection, run_id: UUID, step: str, result: JsonValue
) -> JsonValue:
    """Mark a claimed run step succeeded and return the JSONB-round-tripped result."""
    result = _step_result(result, run_id=run_id, step=step)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE run_steps SET state = 'succeeded', result = %s "
            "WHERE run_id = %s AND step = %s AND state = 'running' RETURNING result",
            (Jsonb(result), run_id, step),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"run_step ({run_id}, {step}) was not running at completion")
    return _step_result(row["result"], run_id=run_id, step=step)


async def abandon_run_step(conn: AsyncConnection, run_id: UUID, step: str) -> None:
    """Drop a running claim so a retry can attempt the side effect again."""
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM run_steps WHERE run_id = %s AND step = %s AND state = 'running'",
            (run_id, step),
        )
