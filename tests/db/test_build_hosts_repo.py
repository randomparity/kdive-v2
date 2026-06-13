"""Tests for the build_hosts repository (ADR-0099).

Uses the same disposable-Postgres pattern as the other db tests: a session-scoped
container via `migrated_url`, one sync test function per scenario, async work
wrapped in `asyncio.run(_run())`.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from kdive.db.build_hosts import (
    WORKER_LOCAL_ID,
    BuildHost,
    get_by_id,
    get_by_name,
    lease_count,
    release_lease,
    try_acquire_lease,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _insert_ssh_host(conn: psycopg.AsyncConnection, *, max_concurrent: int = 2) -> BuildHost:
    """Insert a minimal ssh build host and return the resolved BuildHost."""
    host_id = uuid4()
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
        "workspace_root, max_concurrent) VALUES (%s, %s, 'ssh', '10.0.0.1', "
        "'cred-ref', '/build', %s)",
        (host_id, f"test-ssh-{host_id}", max_concurrent),
    )
    host = await get_by_name(conn, f"test-ssh-{host_id}")
    assert host is not None
    return host


# ---------------------------------------------------------------------------
# Test 1: get_by_name resolves the seeded worker-local row and returns None for unknown
# ---------------------------------------------------------------------------


def test_get_by_name_seeded_row_and_missing(migrated_url: str) -> None:
    """get_by_name('worker-local') returns the seeded BuildHost; unknown name → None."""

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            host = await get_by_name(conn, "worker-local")
            assert host is not None
            assert host.id == WORKER_LOCAL_ID
            assert host.kind == "local"
            assert host.enabled is True
            assert host.state == "ready"
            assert host.address is None
            assert host.ssh_credential_ref is None

            missing = await get_by_name(conn, "nope")
            assert missing is None

    asyncio.run(_run())


def test_get_by_id_seeded_row_and_missing(migrated_url: str) -> None:
    """get_by_id(WORKER_LOCAL_ID) returns the seeded BuildHost; an unknown id → None."""

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            host = await get_by_id(conn, WORKER_LOCAL_ID)
            assert host is not None
            assert host.id == WORKER_LOCAL_ID
            assert host.name == "worker-local"
            assert host.kind == "local"

            missing = await get_by_id(conn, uuid4())
            assert missing is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 2: try_acquire_lease enforces max_concurrent
# ---------------------------------------------------------------------------


def test_try_acquire_lease_enforces_capacity(migrated_url: str) -> None:
    """Two leases on a max_concurrent=2 host succeed; the third is rejected."""

    async def _run() -> None:
        run_a = uuid4()
        run_b = uuid4()
        run_c = uuid4()

        async with await _connect(migrated_url) as conn:
            host = await _insert_ssh_host(conn, max_concurrent=2)

        # Each acquire runs in its own transaction (xact-scoped lock).
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                result_a = await try_acquire_lease(conn, host, run_a)
            assert result_a is True

        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                result_b = await try_acquire_lease(conn, host, run_b)
            assert result_b is True

        # Verify lease count from a separate autocommit connection.
        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host.id) == 2

        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                result_c = await try_acquire_lease(conn, host, run_c)
            assert result_c is False

        # Count must still be 2 after the rejected acquire.
        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host.id) == 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 3: re-acquire for an already-leased run_id is idempotent
# ---------------------------------------------------------------------------


def test_try_acquire_lease_idempotent(migrated_url: str) -> None:
    """Re-acquiring with the same run_id returns True; lease count is unchanged."""

    async def _run() -> None:
        run_id = uuid4()

        async with await _connect(migrated_url) as conn:
            host = await _insert_ssh_host(conn, max_concurrent=2)

        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                first = await try_acquire_lease(conn, host, run_id)
            assert first is True

        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host.id) == 1

        # Re-acquire in a new transaction — must be True, count must stay at 1.
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                second = await try_acquire_lease(conn, host, run_id)
            assert second is True

        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host.id) == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 4: release_lease drops the count and is idempotent
# ---------------------------------------------------------------------------


def test_release_lease_drops_count_and_is_idempotent(migrated_url: str) -> None:
    """release_lease decrements the count; a second call for the same run_id is a no-op."""

    async def _run() -> None:
        run_a = uuid4()
        run_b = uuid4()

        async with await _connect(migrated_url) as conn:
            host = await _insert_ssh_host(conn, max_concurrent=2)

        for run_id in (run_a, run_b):
            async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
                async with conn.transaction():
                    ok = await try_acquire_lease(conn, host, run_id)
                assert ok is True

        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host.id) == 2

        # Release one lease; count drops to 1.
        async with await _connect(migrated_url) as conn:
            await release_lease(conn, run_a)
            assert await lease_count(conn, host.id) == 1

        # Release the same run_id again — no error, count unchanged.
        async with await _connect(migrated_url) as conn:
            await release_lease(conn, run_a)
            assert await lease_count(conn, host.id) == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 5: idempotency is host-scoped — a run leased on host A does not silently
# return True when acquired against host B; it surfaces the run_id PK conflict.
# ---------------------------------------------------------------------------


def test_try_acquire_lease_wrong_host_raises_unique_violation(migrated_url: str) -> None:
    """A run already leased against one host cannot silently re-acquire on another.

    The host-scoped existing-lease check does not match the second host, so the
    call falls through to the INSERT, which hits the run_id primary-key conflict.
    """

    async def _run() -> None:
        run_id = uuid4()

        async with await _connect(migrated_url) as conn:
            host_local = await get_by_name(conn, "worker-local")
            assert host_local is not None
            host_ssh = await _insert_ssh_host(conn, max_concurrent=2)

        # Lease run_id against the seeded local host.
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            async with conn.transaction():
                ok = await try_acquire_lease(conn, host_local, run_id)
            assert ok is True

        # Same run_id, different host: must NOT silently return True. The
        # host-scoped check misses, the INSERT runs, the run_id PK conflicts.
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(psycopg.errors.UniqueViolation):
                async with conn.transaction():
                    await try_acquire_lease(conn, host_ssh, run_id)

        # The original local lease is intact; the ssh host gained nothing.
        async with await _connect(migrated_url) as conn:
            assert await lease_count(conn, host_local.id) == 1
            assert await lease_count(conn, host_ssh.id) == 0

    asyncio.run(_run())
