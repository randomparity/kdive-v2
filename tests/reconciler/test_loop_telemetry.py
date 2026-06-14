"""Reconciler per-pass telemetry + loop-granularity heartbeat (ADR-0090 §5).

The reconciler ticks the ``/livez`` heartbeat once per pass (not per repair) and emits a
per-pass span + duration/lag metrics. These run without a DB by stubbing ``run_once``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.health.heartbeat import Heartbeat
from kdive.providers.reaping import NullReaper
from kdive.reconciler import loop as reconciler_loop
from kdive.reconciler.loop import ReconcileConfig, Reconciler, ReconcileReport
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry


def _empty_report() -> ReconcileReport:
    return ReconcileReport(
        expired_allocations=0,
        orphaned_systems=0,
        abandoned_jobs=0,
        dead_sessions=0,
        leaked_domains=0,
        idempotency_keys_gc_count=0,
        failures=(),
    )


class _CountingHeartbeat:
    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1


def _telemetry() -> tuple[ReconcilerTelemetry, InMemoryMetricReader, InMemorySpanExporter]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return ReconcilerTelemetry(tracer=tp.get_tracer("test"), meter=meter), reader, exporter


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    names: set[str] = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            names.update(m.name for m in sm.metrics)
    return names


def test_disabled_pass_span_is_noop() -> None:
    telemetry = ReconcilerTelemetry.disabled()
    with telemetry.pass_span() as span:
        span.set_outcome("ok")
    telemetry.observe_lag(1.0)  # no meter; must not raise


def test_pass_span_records_duration_and_emits_span() -> None:
    telemetry, reader, exporter = _telemetry()
    with telemetry.pass_span() as span:
        span.set_outcome("ok")
    assert "kdive.reconcile.duration" in _metric_names(reader)
    spans = exporter.get_finished_spans()
    assert spans and spans[0].name == "reconcile/pass"


def test_background_ticker_keeps_livez_live_across_a_long_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that blocks far past stale_after must NOT flip /livez stale (ADR-0090 §5)."""

    async def _run() -> None:
        hb = Heartbeat(stale_after=0.05)
        reconciler = Reconciler(
            pool=_FakePool(),  # ty: ignore[invalid-argument-type]
            reaper=NullReaper(),
            config=ReconcileConfig(
                interval=timedelta(milliseconds=1),
                heartbeat=hb,
                heartbeat_tick=timedelta(milliseconds=5),
            ),
        )
        stop = asyncio.Event()
        live_during_pass: list[bool] = []

        async def long_run_once() -> ReconcileReport:
            await asyncio.sleep(0.2)  # a slow pass far longer than stale_after
            live_during_pass.append(hb.is_live())
            stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", long_run_once)
        await asyncio.wait_for(reconciler.run(stop), timeout=2)
        assert live_during_pass == [True]

    asyncio.run(_run())


def test_background_ticker_does_not_tick_after_stop() -> None:
    async def _run() -> None:
        heartbeat = _CountingHeartbeat()
        stop = asyncio.Event()
        task = asyncio.create_task(
            reconciler_loop._tick_until_stop(
                cast(Heartbeat, heartbeat),
                stop,
                60.0,
            )
        )
        await asyncio.sleep(0)
        assert heartbeat.ticks == 1

        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert heartbeat.ticks == 1

    asyncio.run(_run())


class _FakePool:
    """A stand-in pool; the heartbeat test stubs run_once so the pool is never used."""
