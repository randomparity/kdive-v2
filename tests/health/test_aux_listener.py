"""The auxiliary health/metrics HTTP app (ADR-0090 §5).

Drives the Starlette aux app in-process via ``httpx.ASGITransport`` (no socket): asserts
``/livez`` reflects the heartbeat, ``/readyz`` flips with the probe and returns 503 when
not-ready, and ``/metrics`` renders the process's metrics in Prometheus text exposition.
"""

from __future__ import annotations

import asyncio

import httpx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.health.aux_listener import build_aux_app
from kdive.health.heartbeat import Heartbeat
from kdive.health.probe import BackendCheck, HealthProbe


def _client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # ty: ignore[invalid-argument-type]
    return httpx.AsyncClient(transport=transport, base_url="http://aux")


def test_livez_reflects_heartbeat() -> None:
    async def _run() -> None:
        beats = {"now": 100.0}
        hb = Heartbeat(stale_after=10.0, now=lambda: beats["now"])
        app = build_aux_app(heartbeat=hb, probe=HealthProbe(checks=[]), metric_reader=None)
        async with _client(app) as client:
            assert (await client.get("/livez")).status_code == 200
            beats["now"] = 200.0  # last tick now stale
            assert (await client.get("/livez")).status_code == 503

    asyncio.run(_run())


def test_readyz_ok_when_all_checks_pass() -> None:
    async def _run() -> None:
        async def ok() -> None:
            return None

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=ok)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 200
            assert resp.json()["checks"] == {"pg": True}

    asyncio.run(_run())


def test_readyz_503_when_a_check_fails() -> None:
    async def _run() -> None:
        async def down() -> None:
            raise RuntimeError("pg down")

        probe = HealthProbe(checks=[BackendCheck(name="pg", probe=down)])
        app = build_aux_app(heartbeat=_fresh_hb(), probe=probe, metric_reader=None)
        async with _client(app) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 503
            assert resp.json()["ready"] is False

    asyncio.run(_run())


def test_metrics_renders_recorded_counter() -> None:
    async def _run() -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        counter = provider.get_meter("test").create_counter("kdive_test_total")
        counter.add(3, {"outcome": "ok"})
        app = build_aux_app(
            heartbeat=_fresh_hb(), probe=HealthProbe(checks=[]), metric_reader=reader
        )
        async with _client(app) as client:
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            assert "text/plain" in resp.headers["content-type"]
            assert "kdive_test_total" in resp.text
            assert 'outcome="ok"' in resp.text

    asyncio.run(_run())


def test_metrics_404_when_no_reader() -> None:
    async def _run() -> None:
        app = build_aux_app(heartbeat=_fresh_hb(), probe=HealthProbe(checks=[]), metric_reader=None)
        async with _client(app) as client:
            assert (await client.get("/metrics")).status_code == 404

    asyncio.run(_run())


def _fresh_hb() -> Heartbeat:
    return Heartbeat(stale_after=1e9)
