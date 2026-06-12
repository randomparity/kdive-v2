"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

import kdive.config as config
from kdive.__main__ import build_parser
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.observability import Telemetry
from kdive.reconciler.loop import ReconcileConfig
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"


def test_optional_reconciler_object_store_disables_when_s3_env_absent() -> None:
    from kdive import __main__

    try:
        config.load({})

        def _missing_store() -> ObjectStore:
            raise CategorizedError("missing S3", category=ErrorCategory.CONFIGURATION_ERROR)

        assert __main__._optional_reconciler_object_store(_missing_store) is None
    finally:
        config.reset()


def test_optional_reconciler_object_store_reraises_when_s3_env_partial() -> None:
    from kdive import __main__

    try:
        config.load({"KDIVE_S3_ENDPOINT_URL": "http://localhost:9000"})
        error = CategorizedError("missing bucket", category=ErrorCategory.CONFIGURATION_ERROR)

        def _invalid_store() -> ObjectStore:
            raise error

        with pytest.raises(CategorizedError) as exc:
            __main__._optional_reconciler_object_store(_invalid_store)
        assert exc.value is error
    finally:
        config.reset()


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
    discovery_release = asyncio.Event()

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
    monkeypatch.setattr(
        "kdive.process_health.server.build_postgres_ping", lambda pool: lambda: None
    )

    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover-start")
            await discovery_release.wait()
            events.append("discover-end")

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
        config = cast(ReconcileConfig, kw["config"])
        constructed["resetter"] = config.resetter
        constructed["dump_volume_reaper"] = config.dump_volume_reaper

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")
        discovery_release.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler(expected_registry, _fake_telemetry()))

    assert events[0] == "open"
    assert events[-1] == "close"
    assert "discover-end" in events
    assert events.index("run") < events.index("discover-end")
    assert constructed["reaper"] is expected_reaper
    assert constructed["resetter"] is expected_resetter
    assert constructed["dump_volume_reaper"] is expected_dump_volume_reaper
