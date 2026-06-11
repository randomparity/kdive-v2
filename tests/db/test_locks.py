"""Tests for transaction-scoped advisory locks (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import (
    CONSOLE_HOSTING_LEADER,
    LockScope,
    SessionAdvisoryLock,
    _lock_key,
    _session_lock_key,
    advisory_xact_lock,
)
from tests.db_waits import wait_until_backend_waiting

_KEY = UUID("11111111-1111-1111-1111-111111111111")


def test_lock_key_deterministic_and_scope_sensitive() -> None:
    key = uuid4()
    assert _lock_key(LockScope.ALLOCATION, key) == _lock_key(LockScope.ALLOCATION, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.SYSTEM, key)
    assert _lock_key(LockScope.ALLOCATION, key) != _lock_key(LockScope.ALLOCATION, uuid4())
    value = _lock_key(LockScope.ALLOCATION, key)
    assert -(2**63) <= value < 2**63


def test_resource_scope_key_is_distinct_from_other_scopes() -> None:
    key = UUID("12345678-1234-5678-1234-567812345678")
    resource_key = _lock_key(LockScope.RESOURCE, key)
    assert resource_key != _lock_key(LockScope.ALLOCATION, key)
    assert resource_key != _lock_key(LockScope.SYSTEM, key)
    assert _lock_key(LockScope.RESOURCE, key) == resource_key  # deterministic


def test_investigation_scope_key_is_distinct_from_other_scopes() -> None:
    key = UUID("12345678-1234-5678-1234-567812345678")
    inv_key = _lock_key(LockScope.INVESTIGATION, key)
    assert inv_key != _lock_key(LockScope.ALLOCATION, key)
    assert inv_key != _lock_key(LockScope.SYSTEM, key)
    assert inv_key != _lock_key(LockScope.RESOURCE, key)
    assert _lock_key(LockScope.INVESTIGATION, key) == inv_key  # deterministic


def test_run_scope_key_is_distinct_from_other_scopes() -> None:
    key = UUID("12345678-1234-5678-1234-567812345678")
    run_key = _lock_key(LockScope.RUN, key)
    assert run_key != _lock_key(LockScope.ALLOCATION, key)
    assert run_key != _lock_key(LockScope.SYSTEM, key)
    assert run_key != _lock_key(LockScope.INVESTIGATION, key)
    assert _lock_key(LockScope.RUN, key) == run_key  # deterministic


def test_project_scope_keyed_by_string_is_deterministic_and_distinct() -> None:
    # PROJECT is keyed by the `project` string (ADR-0040), not a UUID. Distinct
    # projects get distinct keys; the same project is deterministic; the value fits
    # the signed 64-bit advisory-lock space.
    key = _lock_key(LockScope.PROJECT, "kernel-team")
    assert key == _lock_key(LockScope.PROJECT, "kernel-team")
    assert key != _lock_key(LockScope.PROJECT, "other-team")
    assert -(2**63) <= key < 2**63


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
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()
                assert not acquired_b.is_set()
            # a's transaction committed above, releasing the lock.
            assert await asyncio.wait_for(task, timeout=5) == "acquired"

    asyncio.run(_run())


def test_project_lock_serializes_two_connections_on_one_project(postgres_url: str) -> None:
    """Two connections taking PROJECT for the same project serialize; the waiter
    unblocks only after the holder's transaction commits (issue ① acceptance)."""
    project = "kernel-team"

    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            acquired_b = asyncio.Event()

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.PROJECT, project):
                    acquired_b.set()
                    return "acquired"

            async with (  # noqa: SIM117
                a.transaction(),
                advisory_xact_lock(a, LockScope.PROJECT, project),
            ):
                task = asyncio.create_task(acquire_b())
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()
                assert not acquired_b.is_set()
            # a committed, releasing the PROJECT lock; b may now proceed.
            assert await asyncio.wait_for(task, timeout=5) == "acquired"

    asyncio.run(_run())


def test_project_lock_does_not_block_distinct_projects(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
            a.transaction(),
            advisory_xact_lock(a, LockScope.PROJECT, "team-a"),
        ):

            async def acquire_b() -> str:
                async with b.transaction(), advisory_xact_lock(b, LockScope.PROJECT, "team-b"):
                    return "acquired"

            assert await asyncio.wait_for(acquire_b(), timeout=5) == "acquired"

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


def test_session_lock_key_deterministic_and_name_sensitive() -> None:
    assert _session_lock_key("a") == _session_lock_key("a")
    assert _session_lock_key("a") != _session_lock_key("b")
    value = _session_lock_key(CONSOLE_HOSTING_LEADER)
    assert -(2**63) <= value < 2**63


def test_session_lock_key_disjoint_from_xact_lock_space() -> None:
    # A session leadership lock and a transaction-scoped scope lock must never collide
    # in the single-bigint advisory space, or leadership would serialize against a
    # per-object op. The session helper salts its key so the two spaces stay disjoint.
    assert _session_lock_key("console-hosting-leader") != _lock_key(
        LockScope.SYSTEM, "console-hosting-leader"
    )


def test_session_lock_held_across_commits(postgres_url: str) -> None:
    """A session-scoped lock outlives transaction boundaries (unlike advisory_xact_lock)."""

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as conn:
            lock = SessionAdvisoryLock(conn, CONSOLE_HOSTING_LEADER)
            assert await lock.try_acquire() is True
            # A committed transaction on the same connection does not drop it.
            async with conn.transaction():
                await conn.execute("SELECT 1")
            assert await lock.is_held() is True
            await lock.release()
            assert await lock.is_held() is False

    asyncio.run(_run())


def test_session_lock_is_held_for_negative_key(postgres_url: str) -> None:
    # blake2b can produce a key whose high bit is set (negative int8); is_held must not
    # mis-detect it through signed-int4 reconstruction. "a" hashes to a negative key.
    assert _session_lock_key("a") < 0

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as conn:
            lock = SessionAdvisoryLock(conn, "a")
            assert await lock.is_held() is False
            assert await lock.try_acquire() is True
            assert await lock.is_held() is True
            await lock.release()
            assert await lock.is_held() is False

    asyncio.run(_run())


def test_session_lock_single_holder(postgres_url: str) -> None:
    """Only one connection holds the session lock; a second try fails without blocking."""

    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as a,
            await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as b,
        ):
            leader = SessionAdvisoryLock(a, CONSOLE_HOSTING_LEADER)
            standby = SessionAdvisoryLock(b, CONSOLE_HOSTING_LEADER)
            assert await leader.try_acquire() is True
            assert await standby.try_acquire() is False
            await leader.release()
            # After the leader releases, the standby can claim leadership.
            assert await standby.try_acquire() is True
            await standby.release()

    asyncio.run(_run())


def test_session_lock_released_on_connection_loss(postgres_url: str) -> None:
    """Closing the holder's connection releases the lock so a standby can acquire it.

    This is the split-brain hazard AC6 guards against: Postgres frees a session lock the
    instant the holding backend disconnects, with no notice to the (now-dead) holder.
    """

    async def _run() -> None:
        a = await psycopg.AsyncConnection.connect(postgres_url, autocommit=True)
        leader = SessionAdvisoryLock(a, CONSOLE_HOSTING_LEADER)
        assert await leader.try_acquire() is True
        await a.close()  # simulate a dropped leader connection
        async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as b:
            standby = SessionAdvisoryLock(b, CONSOLE_HOSTING_LEADER)
            assert await standby.try_acquire() is True
            await standby.release()

    asyncio.run(_run())
