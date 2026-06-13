"""Build-handler transport selection + build-host lease release (Task 11, ADR-0342).

The build handler reads ``build_host_id`` from the BUILD payload and dispatches:

- a ``local`` (worker-local) host runs the resolved runtime builder directly (the historical
  path, byte-for-byte) and touches no lease;
- an ``ssh`` host constructs a transport-bound :class:`RemoteLibvirtBuild` inside the
  materialized-identity context manager and runs it, then releases the capacity lease on a
  committed path so a failure reliably frees the slot.

These tests substitute the transport-bound-builder factory (a module function) so no real ssh
runs, and seed real ``build_hosts``/``build_host_leases`` rows so the committed lease release is
asserted against the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import BuildHost, get_by_name, try_acquire_lease
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.handlers import runs as runs_handlers
from kdive.jobs.payloads import BuildPayload
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration._seed import (
    seed_granted_allocation,
    seed_running_run,
    seed_system,
)
from tests.mcp.systems_support import provider_resolver

_GIT_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": {"git": {"remote": "https://git.example/linux.git", "ref": "v6.9"}},
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


class _RecordingBuilder:
    """A worker-local builder that records build() calls and returns refs."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def build(self, run_id: UUID, profile: object) -> BuildOutput:
        self.calls.append(run_id)
        return BuildOutput(
            kernel_ref=f"proj/runs/{run_id}/kernel",
            debuginfo_ref=f"proj/runs/{run_id}/vmlinux",
            build_id="abcdef0123456789",
        )


class _FailingBuilder:
    """A builder whose build() raises a CategorizedError (BUILD_FAILURE)."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def build(self, run_id: UUID, profile: object) -> BuildOutput:
        self.calls.append(run_id)
        raise CategorizedError("make exited non-zero", category=ErrorCategory.BUILD_FAILURE)


class _FakeTransport:
    """A no-op transport stand-in; never used for real ssh in these tests."""


@contextmanager
def _fake_from_host(host: BuildHost, secret_registry: SecretRegistry):
    """Sync context manager mirroring SshBuildTransport.from_host; yields a fake transport."""
    yield _FakeTransport()


def _ssh_resolver(builder: object):
    """A resolver whose remote-libvirt runtime builder is a RemoteLibvirtBuild instance.

    The handler's ssh path requires ``isinstance(runtime.builder, RemoteLibvirtBuild)``; a
    real ``from_env`` builder satisfies that without spawning make or S3 (its seams only run
    inside ``build()``, which the transport factory replaces).
    """
    return provider_resolver(builder=builder)


async def _seed_run(pool: AsyncConnectionPool, profile: dict[str, Any] | None = None) -> str:
    allocation_id = await seed_granted_allocation(pool)
    system_id = await seed_system(pool, allocation_id, SystemState.READY)
    return await seed_running_run(pool, system_id, build_profile=profile)


async def _seed_ssh_host(pool: AsyncConnectionPool) -> BuildHost:
    host_id = uuid4()
    name = f"ssh-{host_id}"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
            "workspace_root, max_concurrent) VALUES (%s, %s, 'ssh', '10.0.0.1', "
            "'cred-ref', '/build', 2)",
            (host_id, name),
        )
        host = await get_by_name(conn, name)
    assert host is not None
    return host


async def _acquire_lease(pool: AsyncConnectionPool, host: BuildHost, run_id: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        ok = await try_acquire_lease(conn, host, UUID(run_id))
    assert ok


async def _enqueue(pool: AsyncConnectionPool, run_id: str, build_host_id: str | None):
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.BUILD,
            BuildPayload(run_id=run_id, build_host_id=build_host_id),
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{run_id}:build",
        )


async def _run_state(pool: AsyncConnectionPool, run_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


async def _lease_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM build_host_leases WHERE run_id = %s", (run_id,))
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Test 1: local (worker-local) host runs the runtime builder directly
# ---------------------------------------------------------------------------


def test_local_host_uses_runtime_builder_no_transport(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker-local build_host_id runs runtime.builder directly; no transport, no lease."""

    def _boom(*args: object, **kwargs: object):
        raise AssertionError("ssh transport must not be constructed for a local host")

    monkeypatch.setattr(runs_handlers, "ssh_build_transport_from_host", _boom)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool)
            host = await _worker_local_host(pool)
            job = await _enqueue(pool, run_id, str(host.id))
            builder = _RecordingBuilder()
            async with pool.connection() as conn:
                await runs_handlers.build_handler(
                    conn,
                    job,
                    resolver=provider_resolver(builder=builder),
                    secret_registry=SecretRegistry(),
                )
            assert builder.calls == [UUID(run_id)]
            assert await _run_state(pool, run_id) == "succeeded"

    asyncio.run(_run())


async def _worker_local_host(pool: AsyncConnectionPool) -> BuildHost:
    async with pool.connection() as conn:
        host = await get_by_name(conn, "worker-local")
    assert host is not None
    return host


# ---------------------------------------------------------------------------
# Test 2: ssh host success releases the lease (committed)
# ---------------------------------------------------------------------------


def test_ssh_host_success_releases_lease(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ssh build over a fake transport succeeds and the capacity lease is released."""
    transport_builder = _RecordingBuilder()
    captured: dict[str, object] = {}

    def _fake_factory(builder: object, transport: object, **kwargs: object) -> object:
        captured["builder"] = builder
        captured["transport"] = transport
        captured["kwargs"] = kwargs
        return transport_builder

    monkeypatch.setattr(runs_handlers, "ssh_build_transport_from_host", _fake_from_host)
    monkeypatch.setattr(runs_handlers, "build_over_transport", _fake_factory)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _GIT_PROFILE)
            host = await _seed_ssh_host(pool)
            await _acquire_lease(pool, host, run_id)
            assert await _lease_count(pool, run_id) == 1
            job = await _enqueue(pool, run_id, str(host.id))
            async with pool.connection() as conn:
                await runs_handlers.build_handler(
                    conn,
                    job,
                    resolver=_ssh_resolver(
                        RemoteLibvirtBuild.from_env(secret_registry=SecretRegistry())
                    ),
                    secret_registry=SecretRegistry(),
                )
            assert transport_builder.calls == [UUID(run_id)]
            assert isinstance(captured["transport"], _FakeTransport)
            assert await _run_state(pool, run_id) == "succeeded"
            assert await _lease_count(pool, run_id) == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 3: ssh host build failure marks run FAILED but RETAINS the lease
# ---------------------------------------------------------------------------


def test_ssh_host_build_failure_retains_lease(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ssh build marks the run FAILED but RETAINS the lease (retries must not over-admit).

    The build job retries up to ``max_attempts``; releasing the slot between attempts would let
    another build grab it while attempts 2-3 still run on the host. The lease is held until the
    job is terminal, when the reconciler reclaims it.
    """
    failing = _FailingBuilder()
    monkeypatch.setattr(runs_handlers, "ssh_build_transport_from_host", _fake_from_host)
    monkeypatch.setattr(
        runs_handlers, "build_over_transport", lambda builder, transport, **kw: failing
    )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _GIT_PROFILE)
            host = await _seed_ssh_host(pool)
            await _acquire_lease(pool, host, run_id)
            job = await _enqueue(pool, run_id, str(host.id))
            with pytest.raises(CategorizedError):
                async with pool.connection() as conn:
                    await runs_handlers.build_handler(
                        conn,
                        job,
                        resolver=_ssh_resolver(
                            RemoteLibvirtBuild.from_env(secret_registry=SecretRegistry())
                        ),
                        secret_registry=SecretRegistry(),
                    )
            assert failing.calls == [UUID(run_id)]
            assert await _run_state(pool, run_id) == "failed"
            assert await _lease_count(pool, run_id) == 1  # retained for the reconciler

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 4: ssh host but non-remote-libvirt builder -> NOT_IMPLEMENTED
# ---------------------------------------------------------------------------


def test_ssh_host_non_remote_builder_not_implemented(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ssh host selected for a non-remote-libvirt builder fails NOT_IMPLEMENTED; lease retained.

    All handler failures are treated the same — the lease is never released on the failure path.
    This pre-build failure will recur on every retry (a non-remote builder can't use ssh), so the
    run fails all attempts and the reconciler reclaims the lease once the job is dead-lettered.
    """
    monkeypatch.setattr(runs_handlers, "ssh_build_transport_from_host", _fake_from_host)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _GIT_PROFILE)
            host = await _seed_ssh_host(pool)
            await _acquire_lease(pool, host, run_id)
            job = await _enqueue(pool, run_id, str(host.id))
            with pytest.raises(CategorizedError) as exc:
                async with pool.connection() as conn:
                    await runs_handlers.build_handler(
                        conn,
                        job,
                        resolver=provider_resolver(builder=_RecordingBuilder()),
                        secret_registry=SecretRegistry(),
                    )
            assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
            assert await _run_state(pool, run_id) == "failed"
            assert await _lease_count(pool, run_id) == 1  # retained for the reconciler

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 5: host row gone -> INFRASTRUCTURE_FAILURE, run FAILED
# ---------------------------------------------------------------------------


def test_host_row_gone_infrastructure_failure(migrated_url: str) -> None:
    """A build_host_id pointing at no row fails INFRASTRUCTURE_FAILURE and marks the run FAILED.

    The vanished host has no lease (it was the host row itself that disappeared), so there is
    nothing to retain or reclaim here; the run is driven FAILED durably.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _GIT_PROFILE)
            missing_id = str(uuid4())
            job = await _enqueue(pool, run_id, missing_id)
            with pytest.raises(CategorizedError) as exc:
                async with pool.connection() as conn:
                    await runs_handlers.build_handler(
                        conn,
                        job,
                        resolver=provider_resolver(builder=_RecordingBuilder()),
                        secret_registry=SecretRegistry(),
                    )
            assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert await _run_state(pool, run_id) == "failed"
            assert await _lease_count(pool, run_id) == 0

    asyncio.run(_run())
