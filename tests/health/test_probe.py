"""Backend-health probe behavior (ADR-0090 §5).

Covers the caching asymmetry (healthy cached for a TTL, failure reflected
immediately and never cached), per-check timeout (a hung backend reads as down, not as
a stalled probe), partial-failure (one down dependency makes the whole probe
not-ready), and flip/recover.
"""

from __future__ import annotations

import asyncio

from kdive.health import BackendCheck, HealthProbe


def _ok_check(name: str, calls: list[str]) -> BackendCheck:
    async def probe() -> None:
        calls.append(name)

    return BackendCheck(name=name, probe=probe)


def _failing_check(name: str, calls: list[str]) -> BackendCheck:
    async def probe() -> None:
        calls.append(name)
        raise RuntimeError(f"{name} down")

    return BackendCheck(name=name, probe=probe)


def test_all_healthy_is_ready() -> None:
    async def _run() -> None:
        calls: list[str] = []
        probe = HealthProbe(checks=[_ok_check("pg", calls), _ok_check("minio", calls)])
        result = await probe.check()
        assert result.ready is True
        assert result.checks == {"pg": True, "minio": True}

    asyncio.run(_run())


def test_one_down_makes_not_ready_and_names_it() -> None:
    async def _run() -> None:
        calls: list[str] = []
        probe = HealthProbe(checks=[_ok_check("pg", calls), _failing_check("minio", calls)])
        result = await probe.check()
        assert result.ready is False
        assert result.checks == {"pg": True, "minio": False}

    asyncio.run(_run())


def test_healthy_result_is_cached_for_ttl() -> None:
    async def _run() -> None:
        calls: list[str] = []
        probe = HealthProbe(checks=[_ok_check("pg", calls)], healthy_ttl=60.0)
        assert (await probe.check()).ready is True
        assert (await probe.check()).ready is True
        assert calls == ["pg"], "healthy result cached: probe runs once within the TTL"

    asyncio.run(_run())


def test_failing_result_is_reflected_immediately_not_cached() -> None:
    async def _run() -> None:
        calls: list[str] = []
        probe = HealthProbe(checks=[_failing_check("pg", calls)], healthy_ttl=60.0)
        assert (await probe.check()).ready is False
        assert (await probe.check()).ready is False
        assert calls == ["pg", "pg"], "a failing result must never be cached"

    asyncio.run(_run())


def test_flip_then_recover() -> None:
    async def _run() -> None:
        state = {"up": True}
        calls: list[str] = []

        async def probe_fn() -> None:
            calls.append("pg")
            if not state["up"]:
                raise RuntimeError("pg down")

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=probe_fn)], healthy_ttl=0.0)
        assert (await probe.check()).ready is True
        state["up"] = False
        assert (await probe.check()).ready is False
        state["up"] = True
        assert (await probe.check()).ready is True

    asyncio.run(_run())


def test_per_check_timeout_reads_as_down() -> None:
    async def _run() -> None:
        calls: list[str] = []

        async def hang() -> None:
            calls.append("slow")
            await asyncio.sleep(10.0)

        probe = HealthProbe(
            checks=[BackendCheck(name="slow", probe=hang)],
            check_timeout=0.05,
        )
        result = await probe.check()
        assert result.ready is False
        assert result.checks == {"slow": False}

    asyncio.run(_run())
