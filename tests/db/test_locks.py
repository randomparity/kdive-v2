"""Tests for transaction-scoped advisory locks (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock

_KEY = UUID("11111111-1111-1111-1111-111111111111")


def test_lock_key_deterministic_and_scope_sensitive() -> None:
    key = uuid4()
    assert _lock_key(LockScope.ALLOCATION, key) == _lock_key(LockScope.ALLOCATION, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.SYSTEM, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.ALLOCATION, uuid4())
    value = _lock_key(LockScope.ALLOCATION, key)
    assert -(2**63) <= value < 2**63


async def _wait_until_lock_waiting(observer: psycopg.AsyncConnection, waiter_pid: int) -> None:
    """Poll pg_locks until ``waiter_pid`` is blocked on an advisory lock (not merely slow)."""
    for _ in range(200):
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND NOT granted",
            (waiter_pid,),
        )
        if await cur.fetchone() is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the second connection never began waiting on the advisory lock")


def test_lock_blocks_until_holder_commits(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            acquired_b = asyncio.Event()

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.ALLOCATION, _KEY):
                    acquired_b.set()
                    return "acquired"

            # Kept nested (not combined with the connections above) so b outlives a's
            # transaction: a must commit to release the lock while b is still open to
            # observe the unblock — combining would close b at the same instant.
            async with (  # noqa: SIM117
                a.transaction(),
                advisory_xact_lock(a, LockScope.ALLOCATION, _KEY),
            ):
                task = asyncio.create_task(acquire_b())
                await _wait_until_lock_waiting(a, b.info.backend_pid)
                assert not task.done()
                assert not acquired_b.is_set()
            # a's transaction committed above, releasing the lock.
            assert await asyncio.wait_for(task, timeout=5) == "acquired"

    asyncio.run(_run())


def test_different_key_does_not_block(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
            a.transaction(),
            advisory_xact_lock(a, LockScope.ALLOCATION, _KEY),
        ):

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.ALLOCATION, uuid4()):
                    return "acquired"

            assert await asyncio.wait_for(acquire_b(), timeout=5) == "acquired"

    asyncio.run(_run())


def test_different_scope_does_not_block(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
            a.transaction(),
            advisory_xact_lock(a, LockScope.ALLOCATION, _KEY),
        ):

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.SYSTEM, _KEY):
                    return "acquired"

            assert await asyncio.wait_for(acquire_b(), timeout=5) == "acquired"

    asyncio.run(_run())


def test_no_open_transaction_raises(postgres_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as conn:
            with pytest.raises(RuntimeError, match="open transaction"):
                async with advisory_xact_lock(conn, LockScope.ALLOCATION, uuid4()):
                    pass

    asyncio.run(_run())
