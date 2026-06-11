"""The ephemeral-probe-guest ``guest_egress`` check tests (ADR-0091 §3, §4).

Coverage maps to the #270 acceptance bullets:

* a blocked guest->object-store path -> ``fail`` with the open-the-``FORWARD`` fix;
* a healthy path -> ``pass``; a provider/host unreachable -> ``error`` (never a ``fail``);
* concurrent invocations spin **exactly one** guest (single-flight per provider);
* teardown failure does not change the verdict (the reaper is the backstop);
* a presigned URL is never returned/logged in the clear (``redact_presigned``).

The DB-backed marker lifecycle (register/heartbeat/release + the one-live-row-per-provider
fence) runs against the disposable Postgres fixture; the check logic runs over a fake guest.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import CheckStatus
from kdive.diagnostics.egress_probe import (
    EGRESS_FIX,
    EGRESS_ID,
    PROBE_DOMAIN_PREFIX,
    EgressOutcome,
    EgressProbeRegistry,
    GuestEgressCheck,
    ProbeInFlightError,
    SingleFlight,
    redact_presigned,
)


class _FakeGuest:
    """A scripted :class:`ProbeGuest`: records provisions/teardowns, returns a fixed outcome."""

    def __init__(
        self,
        *,
        outcome: EgressOutcome = EgressOutcome.REACHABLE,
        provision_error: Exception | None = None,
        teardown_error: Exception | None = None,
        provision_delay: float = 0.0,
    ) -> None:
        self._outcome = outcome
        self._provision_error = provision_error
        self._teardown_error = teardown_error
        self._provision_delay = provision_delay
        self.provisioned: list[str] = []
        self.torn_down: list[str] = []
        self.exec_urls: list[str] = []

    async def provision(self, domain_name: str) -> None:
        if self._provision_delay:
            await asyncio.sleep(self._provision_delay)
        self.provisioned.append(domain_name)
        if self._provision_error is not None:
            raise self._provision_error

    async def exec_egress(self, domain_name: str, presigned_url: str) -> EgressOutcome:
        self.exec_urls.append(presigned_url)
        return self._outcome

    async def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)
        if self._teardown_error is not None:
            raise self._teardown_error


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=4, open=False)


async def _presigned() -> str:
    return "https://minio.local/bucket/probe?X-Amz-Credential=AKIA&X-Amz-Signature=deadbeef"


def _check(provider: str, guest: _FakeGuest, registry: EgressProbeRegistry) -> GuestEgressCheck:
    return GuestEgressCheck(
        provider=provider,
        guest=guest,
        presigned_url=_presigned,
        registry=registry,
        single_flight=SingleFlight(),
    )


# ---- redaction ----------------------------------------------------------------------


def test_redact_presigned_drops_the_credential_query() -> None:
    url = "https://minio.local/bucket/probe?X-Amz-Credential=AKIA&X-Amz-Signature=deadbeef"
    redacted = redact_presigned(url)
    assert "AKIA" not in redacted
    assert "deadbeef" not in redacted
    assert "Signature" not in redacted
    assert redacted == "https://minio.local/bucket/probe?<redacted>"


# ---- three-state verdict ------------------------------------------------------------


def test_healthy_path_passes(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(outcome=EgressOutcome.REACHABLE)
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            result = await _check("p", guest, registry).run()
        assert result.status is CheckStatus.PASS
        assert result.check_id == EGRESS_ID
        assert result.provider == "p"
        assert result.fix is None
        assert len(guest.provisioned) == 1
        assert guest.torn_down == guest.provisioned  # provisioned guest torn down

    asyncio.run(_run())


def test_blocked_path_fails_with_the_forward_fix(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(outcome=EgressOutcome.BLOCKED)
        async with _pool(migrated_url) as pool:
            result = await _check("p", guest, EgressProbeRegistry(pool)).run()
        assert result.status is CheckStatus.FAIL
        assert result.fix == EGRESS_FIX
        assert "FORWARD" in result.fix

    asyncio.run(_run())


def test_unreachable_guest_is_error_not_fail(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(outcome=EgressOutcome.UNREACHABLE)
        async with _pool(migrated_url) as pool:
            result = await _check("p", guest, EgressProbeRegistry(pool)).run()
        assert result.status is CheckStatus.ERROR
        assert result.fix is None

    asyncio.run(_run())


def test_provision_failure_is_error_and_guest_torn_down(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(provision_error=RuntimeError("no image staged on provider"))
        async with _pool(migrated_url) as pool:
            result = await _check("p", guest, EgressProbeRegistry(pool)).run()
        assert result.status is CheckStatus.ERROR
        assert result.fix is None
        # teardown is still attempted on the (failed) provision path — best-effort cleanup.
        assert len(guest.torn_down) == 1

    asyncio.run(_run())


def test_teardown_failure_does_not_change_verdict(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(
            outcome=EgressOutcome.REACHABLE, teardown_error=RuntimeError("destroy timed out")
        )
        async with _pool(migrated_url) as pool:
            result = await _check("p", guest, EgressProbeRegistry(pool)).run()
        # teardown raised but the verdict stands (the reaper is the backstop).
        assert result.status is CheckStatus.PASS

    asyncio.run(_run())


def test_heartbeat_keeps_beating_for_a_slow_probe(migrated_url: str) -> None:
    # A probe whose boot+exec exceeds the beat interval must keep advancing its heartbeat, or
    # the reaper would reap a live (slow) probe mid-check (ADR-0091 §3). Assert the heartbeat
    # advances more than once during a probe that runs across several beat intervals.
    async def _run() -> None:
        guest = _FakeGuest(outcome=EgressOutcome.REACHABLE, provision_delay=0.15)
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            check = GuestEgressCheck(
                provider="p",
                guest=guest,
                presigned_url=_presigned,
                registry=registry,
                single_flight=SingleFlight(),
                heartbeat_interval=timedelta(seconds=0.03),
            )
            await check.run()
            conn = await pool.getconn()
            try:
                cur = await conn.execute(
                    "SELECT extract(epoch FROM (heartbeat_at - created_at)) "
                    "FROM egress_probe_guests"
                )
                row = await cur.fetchone()
            finally:
                await pool.putconn(conn)
        # The heartbeat advanced past the row's creation time (it beat during the slow probe).
        assert row is not None and row[0] > 0

    asyncio.run(_run())


def test_probe_domain_carries_the_reaper_marker_prefix(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest()
        async with _pool(migrated_url) as pool:
            await _check("p", guest, EgressProbeRegistry(pool)).run()
        assert guest.provisioned[0].startswith(PROBE_DOMAIN_PREFIX)

    asyncio.run(_run())


# ---- single-flight ------------------------------------------------------------------


def test_concurrent_callers_spin_exactly_one_guest(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest(outcome=EgressOutcome.REACHABLE, provision_delay=0.05)
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            single_flight = SingleFlight()
            check = GuestEgressCheck(
                provider="p",
                guest=guest,
                presigned_url=_presigned,
                registry=registry,
                single_flight=single_flight,
            )
            results = await asyncio.gather(check.run(), check.run(), check.run())
        assert all(r.status is CheckStatus.PASS for r in results)
        # Exactly one guest spun despite three concurrent callers.
        assert len(guest.provisioned) == 1

    asyncio.run(_run())


def test_registry_raises_in_flight_when_a_live_row_exists(migrated_url: str) -> None:
    # The DB partial unique index is the cross-process single-flight fence: a second register
    # for a provider with a live (unreleased) row raises ProbeInFlightError, not a generic DB
    # error, so the check can report "already in flight" distinctly.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            await registry.register("p", "kdive-egress-probe-a")
            with pytest.raises(ProbeInFlightError):
                await registry.register("p", "kdive-egress-probe-b")

    asyncio.run(_run())


def test_in_flight_register_is_reported_as_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            await registry.register("p", "kdive-egress-probe-held")  # occupy the live slot
            # A fresh SingleFlight (the cross-process case): register hits the fence -> error.
            result = await _check("p", _FakeGuest(), registry).run()
        assert result.status is CheckStatus.ERROR
        assert "in flight" in result.detail
        assert result.fix is None

    asyncio.run(_run())


def test_marker_row_released_so_next_run_can_provision(migrated_url: str) -> None:
    async def _run() -> None:
        guest = _FakeGuest()
        async with _pool(migrated_url) as pool:
            registry = EgressProbeRegistry(pool)
            await _check("p", guest, registry).run()
            # Sequential second run: the first row was released, so the live-row fence is free.
            await _check("p", guest, registry).run()
            conn = await pool.getconn()
            try:
                cur = await conn.execute("SELECT count(*) FROM egress_probe_guests")
                row = await cur.fetchone()
                cur2 = await conn.execute(
                    "SELECT count(*) FROM egress_probe_guests WHERE released_at IS NULL"
                )
                live = await cur2.fetchone()
            finally:
                await pool.putconn(conn)
        assert row is not None and row[0] == 2  # two runs, two rows
        assert live is not None and live[0] == 0  # both released

    asyncio.run(_run())
