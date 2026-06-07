"""Database wait-state probes for concurrency tests."""

from __future__ import annotations

import asyncio
import time

import psycopg


async def wait_until_backend_waiting(
    observer: psycopg.AsyncConnection,
    waiter_pid: int,
    *,
    locktype: str | None = None,
    timeout_s: float = 5.0,
) -> None:
    """Poll pg_locks until a backend is blocked on a database lock."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=waiter_pid, locktype=locktype):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("backend never began waiting on the expected database lock")


async def wait_until_any_backend_waiting(
    observer: psycopg.AsyncConnection,
    *,
    locktype: str | None = None,
    timeout_s: float = 5.0,
) -> None:
    """Poll pg_locks until some backend is blocked on a database lock."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _has_waiting_lock(observer, waiter_pid=None, locktype=locktype):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no backend began waiting on the expected database lock")


async def _has_waiting_lock(
    observer: psycopg.AsyncConnection,
    *,
    waiter_pid: int | None,
    locktype: str | None,
) -> bool:
    if waiter_pid is not None and locktype is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE NOT granted AND pid = %s AND locktype = %s LIMIT 1",
            (waiter_pid, locktype),
        )
    elif waiter_pid is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE NOT granted AND pid = %s LIMIT 1",
            (waiter_pid,),
        )
    elif locktype is not None:
        cur = await observer.execute(
            "SELECT 1 FROM pg_locks WHERE NOT granted AND locktype = %s LIMIT 1",
            (locktype,),
        )
    else:
        cur = await observer.execute("SELECT 1 FROM pg_locks WHERE NOT granted LIMIT 1")
    return await cur.fetchone() is not None
