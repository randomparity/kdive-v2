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
*stored result*, not a single *fn execution*. A caller whose `fn` has a side effect
that must run at most once must serialize the whole `run_step` under an external lock
keyed on the step's scope — the sole production caller holds `LockScope.RUN`
(`mcp/tools/runs.py::_run_step_locked`). See
`tests/adversarial/test_idempotency_concurrency.py` for both facts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


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
            return existing["result"]
        result = await fn()
        await cur.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, %s, 'succeeded', %s) "
            "ON CONFLICT (run_id, step) DO NOTHING RETURNING result",
            (run_id, step, Jsonb(result)),
        )
        inserted = await cur.fetchone()
        if inserted is not None:
            return inserted["result"]
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = %s", (run_id, step)
        )
        winner = await cur.fetchone()
    if winner is None:  # Invariant: ON CONFLICT fired, so a committed row exists.
        raise RuntimeError(f"run_step ({run_id}, {step}) conflicted but found no row")
    return winner["result"]
