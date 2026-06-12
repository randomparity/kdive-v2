"""CLI wiring for `python -m kdive worker`: aux listener + readiness + telemetry (#267)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.observability import Telemetry
from kdive.security.secrets.secret_registry import SecretRegistry


def _fake_telemetry() -> Telemetry:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider

    reader = InMemoryMetricReader()
    return Telemetry(
        logger_provider=LoggerProvider(),
        tracer_provider=TracerProvider(),
        meter_provider=MeterProvider(metric_readers=[reader]),
        scrape_reader=reader,
    )


def test_run_worker_wires_heartbeat_readiness_and_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_worker` opens a pool, builds a Worker with the aux stack, runs, closes."""
    from kdive import __main__
    from kdive.jobs import worker as worker_module

    events: list[str] = []

    class _FakePool:
        async def open(self) -> None:
            events.append("open")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(__main__, "create_pool", lambda **kw: _FakePool())
    monkeypatch.setattr(__main__, "_install_stop", lambda: asyncio.Event())
    monkeypatch.setattr("kdive.mcp.app.build_handler_registry", lambda **kw: object())
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: object())
    monkeypatch.setattr(
        "kdive.process_health.server.build_postgres_ping", lambda pool: lambda: None
    )

    async def _no_serve(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr("kdive.health.serve_aux", _no_serve)

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, registry: object, **kw: object) -> None:
        constructed.update(kw)

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")

    monkeypatch.setattr(worker_module.Worker, "__init__", _fake_init)
    monkeypatch.setattr(worker_module.Worker, "run", _fake_run)

    asyncio.run(__main__._run_worker(SecretRegistry(), _fake_telemetry()))

    assert events == ["open", "run", "close"]
    assert constructed["heartbeat"] is not None
    assert constructed["readiness"] is not None
    assert constructed["telemetry"] is not None
