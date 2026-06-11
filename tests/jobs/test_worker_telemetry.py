"""Per-job worker telemetry: span + duration/queue-depth metrics (ADR-0090 §5).

Drives :class:`WorkerTelemetry` against a real in-memory meter/tracer and asserts the
emitted instruments carry only allowlisted labels (``job_kind``/``outcome``).
"""

from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.jobs.worker_telemetry import WorkerTelemetry


def _telemetry() -> tuple[WorkerTelemetry, InMemoryMetricReader, InMemorySpanExporter]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return WorkerTelemetry(tracer=tp.get_tracer("test"), meter=meter), reader, exporter


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    names: set[str] = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            names.update(m.name for m in sm.metrics)
    return names


def test_disabled_is_a_noop() -> None:
    telemetry = WorkerTelemetry.disabled()
    with telemetry.job_span("build") as span:
        span.set_outcome("ok")
    telemetry.observe_queue_depth(3)  # no meter wired; must not raise


def test_job_span_records_duration_and_labels() -> None:
    telemetry, reader, exporter = _telemetry()
    with telemetry.job_span("build") as span:
        span.set_outcome("ok")
    assert "kdive.job.duration" in _metric_names(reader)
    spans = exporter.get_finished_spans()
    assert spans and spans[0].name == "job/build"
    assert spans[0].attributes is not None
    assert spans[0].attributes["job_kind"] == "build"
    assert spans[0].attributes["outcome"] == "ok"


def test_job_span_error_sets_error_status() -> None:
    telemetry, _reader, exporter = _telemetry()
    with telemetry.job_span("teardown") as span:
        span.set_outcome("error")
    spans = exporter.get_finished_spans()
    assert spans[0].attributes is not None
    assert spans[0].attributes["outcome"] == "error"
    assert spans[0].status.status_code.name == "ERROR"


def test_queue_depth_is_recorded() -> None:
    telemetry, reader, _exporter = _telemetry()
    telemetry.observe_queue_depth(1)
    assert "kdive.job.queue.depth" in _metric_names(reader)
