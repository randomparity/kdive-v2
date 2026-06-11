"""Reconciler per-pass telemetry + loop-granularity heartbeat (ADR-0090 §5).

The reconciler ticks the ``/livez`` heartbeat once per pass (not per repair) and emits a
per-pass span + duration/lag metrics. These run without a DB by stubbing ``run_once``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.health import Heartbeat
from kdive.providers.reaping import NullReaper
from kdive.reconciler.loop import Reconciler, ReconcileReport
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


def test_run_ticks_heartbeat_each_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        clock = {"now": 0.0}
        hb = Heartbeat(stale_after=10.0, now=lambda: clock["now"])
        reconciler = Reconciler(
            pool=_FakePool(),  # ty: ignore[invalid-argument-type]
            reaper=NullReaper(),
            interval=timedelta(milliseconds=1),
            heartbeat=hb,
        )
        stop = asyncio.Event()
        passes = 0

        async def fake_run_once() -> ReconcileReport:
            nonlocal passes
            passes += 1
            clock["now"] += 5.0  # a slow pass; the next loop pass re-ticks
            if passes >= 3:
                stop.set()
            return _empty_report()

        monkeypatch.setattr(reconciler, "run_once", fake_run_once)
        await asyncio.wait_for(reconciler.run(stop), timeout=2)
        assert hb.is_live()

    asyncio.run(_run())


class _FakePool:
    """A stand-in pool; the heartbeat test stubs run_once so the pool is never used."""
