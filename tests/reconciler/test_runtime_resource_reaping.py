"""Tests for runtime-resource lease + reachability reaping (M2.6 #398, ADR-0112).

A ``managed_by='runtime'`` resource whose ``lease_expires_at`` has lapsed is reaped (its row
deleted) when idle, or **cordoned** (never destroyed) when it still backs a live allocation —
the cordon-only / refuse-if-live contract that also preserves a ``crashed`` System under live
crash-debug (ADR-0109). Config/discovery rows carry no lease and are never lease-reaped.

Seeding uses autocommit connections; the repair runs through a real non-autocommit pool
(mirrors test_build_hosts.py). Leases are written relative to the DB clock so there is no
test-vs-Postgres skew.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.providers.reaping import NullReaper
from kdive.reconciler import loop
from kdive.reconciler.loop import reconcile_once
from kdive.reconciler.runtime_resources import reap_expired_runtime_resources
from tests.reconciler.conftest import connect, run_repair


async def _seed_resource(
    conn: psycopg.AsyncConnection,
    *,
    managed_by: str,
    lease_seconds: int | None,
    host_uri: str = "qemu+tls://host.example/system",
    kind: str = "remote-libvirt",
) -> UUID:
    """Insert a resource; ``lease_expires_at = now() + lease_seconds`` (negative = lapsed).

    ``lease_seconds=None`` leaves the lease NULL (the config/discovery case). Raw SQL because
    the lease is relative to the DB clock and a relative interval cannot be a bound param.
    """
    resource_id = uuid4()
    lease_sql = "NULL" if lease_seconds is None else "now() + make_interval(secs => %(secs)s)"
    await conn.execute(
        "INSERT INTO resources (id, kind, name, capabilities, pool, cost_class, status, "
        "    host_uri, managed_by, lease_expires_at) "
        f"VALUES (%(id)s, %(kind)s, %(name)s, %(caps)s, 'default', 'c', 'available', "
        f"    %(host_uri)s, %(managed_by)s, {lease_sql})",
        {
            "id": resource_id,
            "kind": kind,
            "name": f"res-{resource_id}",
            "caps": Jsonb({}),
            "host_uri": host_uri,
            "managed_by": managed_by,
            "secs": lease_seconds,
        },
    )
    return resource_id


async def _seed_live_allocation(conn: psycopg.AsyncConnection, resource_id: UUID) -> None:
    """Insert an ``active`` (non-terminal) allocation backed by ``resource_id``."""
    await conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (%s, 'alice', 'proj', %s, 'active')",
        (uuid4(), resource_id),
    )


async def _exists(conn: psycopg.AsyncConnection, resource_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM resources WHERE id = %s", (resource_id,))
    return await cur.fetchone() is not None


async def _cordoned(conn: psycopg.AsyncConnection, resource_id: UUID) -> bool:
    cur = await conn.execute("SELECT cordoned FROM resources WHERE id = %s", (resource_id,))
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


class _FakeProbe:
    """A ResourceProbe stand-in: maps host_uri -> reachable bool; records every probe."""

    def __init__(self, results: dict[str, bool]) -> None:
        self._results = results
        self.probed: list[str] = []

    async def probe(self, host_uri: str) -> bool:
        self.probed.append(host_uri)
        return self._results.get(host_uri, False)


class _RaisingProbe:
    async def probe(self, host_uri: str) -> bool:
        raise RuntimeError(f"probe failed for {host_uri}")


# ---------------------------------------------------------------------------
# Lease-expiry reaping (the primary contract + invariant 6)
# ---------------------------------------------------------------------------


def test_lapsed_lease_idle_runtime_resource_is_reaped(migrated_url: str) -> None:
    """A runtime resource past its lease with no live allocation is reaped (row deleted)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=-60)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reap_expired_runtime_resources)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _exists(check, res)

    asyncio.run(_run())


def test_lapsed_lease_live_runtime_resource_is_cordoned_not_destroyed(migrated_url: str) -> None:
    """A lapsed-lease runtime resource with a live allocation is cordoned, never destroyed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=-60)
            await _seed_live_allocation(seed, res)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reap_expired_runtime_resources)

        assert count == 1  # cordon is a surfaced action
        async with await connect(migrated_url) as check:
            assert await _exists(check, res)  # never destroyed
            assert await _cordoned(check, res)

    asyncio.run(_run())


def test_cordon_is_change_detecting_no_steady_state_drift(migrated_url: str) -> None:
    """A still-cordoned live lapsed resource is a no-op on the next pass (count 0, no drift).

    The candidate (lease lapsed + live allocation) persists every pass, but only the pass that
    actually flips ``cordoned`` should count — else the reap count never returns to steady-state
    zero (the ReconcileDiff change-detecting convention).
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=-60)
            await _seed_live_allocation(seed, res)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, reap_expired_runtime_resources)
            second = await run_repair(pool, reap_expired_runtime_resources)

        assert first == 1  # the cordoning pass
        assert second == 0  # already cordoned; no phantom drift
        async with await connect(migrated_url) as check:
            assert await _exists(check, res)
            assert await _cordoned(check, res)

    asyncio.run(_run())


def test_unexpired_lease_runtime_resource_is_left_alone(migrated_url: str) -> None:
    """A runtime resource whose lease is still in the future is never a candidate."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=3600)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reap_expired_runtime_resources)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _exists(check, res)
            assert not await _cordoned(check, res)

    asyncio.run(_run())


def test_config_row_with_no_lease_is_never_reaped(migrated_url: str) -> None:
    """A config row (managed_by='config', NULL lease) is never lease-reaped."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="config", lease_seconds=None)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reap_expired_runtime_resources)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _exists(check, res)

    asyncio.run(_run())


def test_discovery_row_with_no_lease_is_never_reaped(migrated_url: str) -> None:
    """A discovery row (managed_by='discovery', NULL lease) is never lease-reaped."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(
                seed, managed_by="discovery", lease_seconds=None, kind="local-libvirt"
            )

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reap_expired_runtime_resources)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _exists(check, res)

    asyncio.run(_run())


def test_reap_is_idempotent(migrated_url: str) -> None:
    """Running the reaper twice is safe; the second pass returns 0."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, managed_by="runtime", lease_seconds=-60)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, reap_expired_runtime_resources)
            second = await run_repair(pool, reap_expired_runtime_resources)

        assert first == 1
        assert second == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Reachability confirmation (the optional probe never gates reaping)
# ---------------------------------------------------------------------------


def test_unreachable_lapsed_runtime_resource_reaped_and_probed(migrated_url: str) -> None:
    """An unreachable lapsed-lease runtime resource is reaped; the probe confirms/logs it."""

    async def _run() -> None:
        host = "qemu+tls://gone.example/system"
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=-60, host_uri=host)
        probe = _FakeProbe({host: False})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_expired_runtime_resources(c, probe))

        assert count == 1
        assert probe.probed == [host]
        async with await connect(migrated_url) as check:
            assert not await _exists(check, res)

    asyncio.run(_run())


def test_reachable_lapsed_runtime_resource_still_reaped(migrated_url: str) -> None:
    """A still-reachable but lapsed-lease runtime resource is reaped (the agent abandoned it)."""

    async def _run() -> None:
        host = "qemu+tls://up.example/system"
        async with await connect(migrated_url) as seed:
            res = await _seed_resource(seed, managed_by="runtime", lease_seconds=-60, host_uri=host)
        probe = _FakeProbe({host: True})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_expired_runtime_resources(c, probe))

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _exists(check, res)

    asyncio.run(_run())


def test_unexpired_lease_is_not_probed(migrated_url: str) -> None:
    """A resource with a future lease is never a candidate, so it is never probed (no flap-reap)."""

    async def _run() -> None:
        host = "qemu+tls://flap.example/system"
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, managed_by="runtime", lease_seconds=3600, host_uri=host)
        probe = _FakeProbe({host: False})  # transiently unreachable but still leased

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_expired_runtime_resources(c, probe))

        assert count == 0
        assert probe.probed == []

    asyncio.run(_run())


def test_reap_candidate_failure_logs_exception_context(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed per-resource probe is isolated and logged with traceback context."""

    async def _run() -> None:
        host = "qemu+tls://boom.example/system"
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, managed_by="runtime", lease_seconds=-60, host_uri=host)

        caplog.set_level(logging.WARNING, logger="kdive.reconciler.runtime_resources")
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, lambda c: reap_expired_runtime_resources(c, _RaisingProbe())
            )

        assert count == 0
        warnings = [
            record for record in caplog.records if "reaping runtime resource" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert isinstance(warnings[0].exc_info[1], RuntimeError)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Loop wiring
# ---------------------------------------------------------------------------


def test_reap_spec_present_in_plan_without_probe() -> None:
    """The reap repair is in the plan unconditionally (lease-expiry reaping needs no probe)."""
    from datetime import timedelta

    plan = loop._repair_plan(
        reaper=NullReaper(),
        config=loop.ReconcileConfig(),
        image_publish_grace=timedelta(minutes=5),
    )
    names = [spec.name for spec in plan]
    assert "reaped_runtime_resources" in names


def test_reap_spec_registered_in_loop() -> None:
    """The _reap_expired_runtime_resources alias is exported from the loop module."""
    assert "_reap_expired_runtime_resources" in loop.__all__
    assert callable(loop._reap_expired_runtime_resources)


def test_reconcile_once_reports_reaped_runtime_resources(migrated_url: str) -> None:
    """reconcile_once surfaces the reap count in its report."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_resource(seed, managed_by="runtime", lease_seconds=-60)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())

        assert report.reaped_runtime_resources == 1

    asyncio.run(_run())
