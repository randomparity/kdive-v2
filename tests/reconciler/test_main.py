"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.__main__ import build_parser
from kdive.observability import Telemetry
from kdive.security.secrets.secret_registry import SecretRegistry


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"


def _fake_telemetry() -> Telemetry:
    """A Telemetry the reconciler runner reaches for (providers + scrape reader)."""
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


def test_run_reconciler_builds_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_reconciler` opens a pool, constructs a Reconciler, runs, closes."""
    from kdive import __main__
    from kdive.providers import composition
    from kdive.reconciler import loop

    events: list[str] = []

    class _FakePool:
        async def open(self) -> None:
            events.append("open")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(__main__, "create_pool", lambda **kw: _FakePool())
    monkeypatch.setattr(__main__, "_install_stop", lambda: __import__("asyncio").Event())
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: object())

    async def _no_serve(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr("kdive.health.serve_aux", _no_serve)
    monkeypatch.setattr("kdive.server_health.build_postgres_ping", lambda pool: lambda: None)

    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover")

    expected_reaper = object()
    expected_resetter = object()
    expected_dump_volume_reaper = object()
    expected_registry = SecretRegistry()

    class _FakeProviderComposition:
        def __init__(self, *, secret_registry: SecretRegistry | None = None) -> None:
            assert secret_registry is expected_registry

        def build_provider_resolver(self) -> _FakeResolver:
            return _FakeResolver()

        def build_reconciler_reaper(self) -> object:
            return expected_reaper

        def build_reconciler_transport_resetter(self) -> object:
            return expected_resetter

        def build_reconciler_dump_volume_reaper(self) -> object:
            return expected_dump_volume_reaper

    monkeypatch.setattr(composition, "ProviderComposition", _FakeProviderComposition)

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, reaper: object, **kw: object) -> None:
        constructed["reaper"] = reaper
        constructed["resetter"] = kw.get("resetter")
        constructed["dump_volume_reaper"] = kw.get("dump_volume_reaper")

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler(expected_registry, _fake_telemetry()))

    assert events == ["open", "discover", "run", "close"]
    assert constructed["reaper"] is expected_reaper
    assert constructed["resetter"] is expected_resetter
    assert constructed["dump_volume_reaper"] is expected_dump_volume_reaper
