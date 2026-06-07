"""Tests for transaction-scoped advisory locks (ADR-0005, ADR-0016)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.locks import LockScope, _lock_key, advisory_xact_lock
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
