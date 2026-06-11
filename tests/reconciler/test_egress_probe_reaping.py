"""Heartbeat-honoring reaping of leaked ``guest_egress`` probe guests (ADR-0091 §3).

The headline acceptance: a **leaked** probe (its owning doctor run stopped beating, or it is
past TTL) is reaped, and an **in-use** probe (a live run still beating) is **never** reaped.
The marker rows are seeded directly in ``egress_probe_guests`` with the heartbeat/TTL set via
the DB clock so there is no test-vs-Postgres skew.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.reconciler.provider_reaping import repair_leaked_probe_guests
from tests.reconciler.conftest import connect, run_repair


class _FakeProbeReaper:
    """Records ``destroy`` calls; ``fail_on`` names raise (after being recorded)."""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self._fail_on = fail_on
        self.destroyed: list[str] = []

    async def destroy(self, name: str) -> None:
        self.destroyed.append(name)
        if name in self._fail_on:
            raise RuntimeError(f"destroy of {name} failed")


async def _seed_probe(
    url: str,
    *,
    domain_name: str,
    provider: str = "remote-libvirt",
    heartbeat_ago: timedelta,
    ttl_in: timedelta,
    released: bool = False,
) -> UUID:
    conn = await connect(url)
    async with conn:
        cur = await conn.execute(
            "INSERT INTO egress_probe_guests "
            "  (provider, domain_name, heartbeat_at, ttl_deadline, released_at) "
            "VALUES (%s, %s, now() - %s, now() + %s, %s) RETURNING id",
            (
                provider,
                domain_name,
                heartbeat_ago,
                ttl_in,
                "now()" if released else None,
            ),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


def _reap(reaper: _FakeProbeReaper):
    return lambda conn: repair_leaked_probe_guests(conn, reaper)


def test_stale_heartbeat_probe_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        await _seed_probe(
            migrated_url,
            domain_name="kdive-egress-probe-leak",
            heartbeat_ago=timedelta(minutes=30),  # run stopped beating
            ttl_in=timedelta(minutes=30),  # well within TTL — staleness, not TTL, reaps it
        )
        reaper = _FakeProbeReaper()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 1
        assert reaper.destroyed == ["kdive-egress-probe-leak"]

    asyncio.run(_run())


def test_live_run_probe_is_never_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        await _seed_probe(
            migrated_url,
            domain_name="kdive-egress-probe-live",
            heartbeat_ago=timedelta(seconds=1),  # owning run still beating
            ttl_in=timedelta(minutes=10),
        )
        reaper = _FakeProbeReaper()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []  # a live run is never reaped (heartbeat honored)

    asyncio.run(_run())


def test_ttl_expired_probe_is_reaped_even_if_recently_beating(migrated_url: str) -> None:
    async def _run() -> None:
        # The hard TTL is the backstop: a probe past TTL is reaped even with a fresh heartbeat
        # (a wedged run that beats but never finishes still bounds its cost).
        await _seed_probe(
            migrated_url,
            domain_name="kdive-egress-probe-ttl",
            heartbeat_ago=timedelta(seconds=1),
            ttl_in=timedelta(minutes=-1),  # already past TTL
        )
        reaper = _FakeProbeReaper()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 1
        assert reaper.destroyed == ["kdive-egress-probe-ttl"]

    asyncio.run(_run())


def test_released_probe_is_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        await _seed_probe(
            migrated_url,
            domain_name="kdive-egress-probe-done",
            heartbeat_ago=timedelta(minutes=30),
            ttl_in=timedelta(minutes=-1),
            released=True,  # already torn down by the check's own teardown
        )
        reaper = _FakeProbeReaper()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_reaped_probe_is_marked_released_so_repass_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        await _seed_probe(
            migrated_url,
            domain_name="kdive-egress-probe-leak2",
            heartbeat_ago=timedelta(minutes=30),
            ttl_in=timedelta(minutes=30),
        )
        reaper = _FakeProbeReaper()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, _reap(reaper))
            second = await run_repair(pool, _reap(reaper))
        assert first == 1
        assert second == 0  # the reaped row was stamped released; the re-pass reaps nothing
        assert reaper.destroyed == ["kdive-egress-probe-leak2"]  # destroyed exactly once

    asyncio.run(_run())


def test_one_destroy_failure_does_not_strand_the_others(migrated_url: str) -> None:
    async def _run() -> None:
        await _seed_probe(
            migrated_url,
            domain_name="probe-bad",
            heartbeat_ago=timedelta(minutes=30),
            ttl_in=timedelta(minutes=30),
        )
        await _seed_probe(
            migrated_url,
            domain_name="probe-good",
            provider="local-libvirt",
            heartbeat_ago=timedelta(minutes=30),
            ttl_in=timedelta(minutes=30),
        )
        reaper = _FakeProbeReaper(fail_on=frozenset({"probe-bad"}))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert sorted(reaper.destroyed) == ["probe-bad", "probe-good"]  # both attempted
        assert count == 1  # only the successful destroy is counted

    asyncio.run(_run())
