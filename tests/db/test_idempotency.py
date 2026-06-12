"""Tests for the idempotent step ledger (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from typing import Any, LiteralString
from uuid import UUID

import psycopg
import pytest

from kdive.db.idempotency import JsonValue, run_step


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_run(conn: psycopg.AsyncConnection) -> UUID:
    """Insert the resource->allocation->system + investigation -> run FK chain."""

    async def _ins(query: LiteralString, params: tuple[object, ...] = ()) -> Any:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
        assert row is not None
        return row[0]

    rid = await _ins(
        "INSERT INTO resources (kind, pool, cost_class, status, host_uri) "
        "VALUES ('local-libvirt', 'p', 'c', 'available', 'qemu:///system') RETURNING id"
    )
    aid = await _ins(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'requested', 'alice', 'proj') RETURNING id",
        (rid,),
    )
    sid = await _ins(
        "INSERT INTO systems (allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, 'defined', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (aid,),
    )
    iid = await _ins(
        "INSERT INTO investigations (title, state, principal, project) "
        "VALUES ('t', 'open', 'alice', 'proj') RETURNING id"
    )
    return await _ins(
        "INSERT INTO runs (investigation_id, system_id, state, build_profile, principal, project) "
        "VALUES (%s, %s, 'created', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (iid, sid),
    )


def test_runs_fn_once_across_replays(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> JsonValue:
                nonlocal calls
                calls += 1
                return {"v": 1}

            first = await run_step(conn, run_id, "build", fn)
            second = await run_step(conn, run_id, "build", fn)
            assert first == {"v": 1}
            assert second == {"v": 1}
            assert calls == 1

    asyncio.run(_run_test())


def test_none_result_is_recorded(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> None:
                nonlocal calls
                calls += 1
                return None

            assert await run_step(conn, run_id, "s", fn) is None
            assert await run_step(conn, run_id, "s", fn) is None
            assert calls == 1

    asyncio.run(_run_test())


def test_non_json_result_is_rejected_before_storage(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def fn() -> Any:
                nonlocal calls
                calls += 1
                return (1, 2)

            async def ok() -> JsonValue:
                return [1, 2]

            with pytest.raises(ValueError, match="non-JSON value tuple"):
                await run_step(conn, run_id, "t", fn)
            assert await run_step(conn, run_id, "t", ok) == [1, 2]
            assert calls == 1

    asyncio.run(_run_test())


def test_distinct_steps_are_independent(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)

            async def fn_a() -> JsonValue:
                return {"step": "a"}

            async def fn_b() -> JsonValue:
                return {"step": "b"}

            assert await run_step(conn, run_id, "a", fn_a) == {"step": "a"}
            assert await run_step(conn, run_id, "b", fn_b) == {"step": "b"}

    asyncio.run(_run_test())


def test_failed_fn_is_not_recorded(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            calls = 0

            async def boom() -> JsonValue:
                nonlocal calls
                calls += 1
                raise ValueError("boom")

            async def ok() -> JsonValue:
                nonlocal calls
                calls += 1
                return {"ok": True}

            with pytest.raises(ValueError, match="boom"):
                await run_step(conn, run_id, "step", boom)
            assert await run_step(conn, run_id, "step", ok) == {"ok": True}
            assert calls == 2

    asyncio.run(_run_test())


def test_concurrent_first_call_resolves_to_one_result(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as setup:
            run_id = await _seed_run(setup)
        async with (
            await _connect(migrated_url) as a,
            await _connect(migrated_url) as b,
        ):

            async def go(conn: psycopg.AsyncConnection, tag: str) -> Any:
                async def fn() -> JsonValue:
                    await asyncio.sleep(0.05)  # widen the race so both miss the cache
                    return {"by": tag}

                return await run_step(conn, run_id, "race", fn)

            results = await asyncio.gather(go(a, "a"), go(b, "b"))
        assert results[0] == results[1]  # both return the committed winner's value

    asyncio.run(_run_test())
