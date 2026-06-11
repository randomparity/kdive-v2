"""Three-signal redaction at the OTel SDK boundary (ADR-0090 §4).

A registered secret placed in a log body, a span attribute, and a metric label must
be scrubbed in every exporter's output — the failure mode the single dedicated test
guards against is "logs are clean so we assumed traces were too."
"""

from __future__ import annotations

import logging

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.observability import redaction as orx
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry

_SECRET = "sk-super-secret-value"  # pragma: allowlist secret - test fixture value


def _registry() -> SecretRegistry:
    registry = SecretRegistry()
    registry.register(_SECRET, scope=None)
    return registry


def test_secret_in_log_body_is_redacted_before_export() -> None:
    registry = _registry()
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(orx.RedactingLogProcessor(registry))
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=provider)
    logger = logging.getLogger("kdive.test.redact.log")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("connecting with %s now", _SECRET)
    provider.force_flush()

    bodies = [str(r.log_record.body) for r in exporter.get_finished_logs()]
    assert bodies, "expected an exported log record"
    assert _SECRET not in "".join(bodies)
    assert REDACTION in "".join(bodies)


def test_secret_in_span_attribute_is_redacted_before_export() -> None:
    registry = _registry()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(orx.RedactingSpanExporter(exporter, registry)))
    tracer = provider.get_tracer("kdive.test.redact.span")

    with tracer.start_as_current_span("op") as span:
        span.set_attribute("connection_url", f"grpc://user:{_SECRET}@host:4317")

    spans = exporter.get_finished_spans()
    assert spans, "expected an exported span"
    rendered = str(dict(spans[0].attributes or {}))
    assert _SECRET not in rendered
    assert REDACTION in rendered


class _CapturingMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.captured: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs
    ) -> MetricExportResult:
        self.captured.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30000, **kwargs) -> None:
        return None


def test_secret_in_metric_label_is_redacted_before_export() -> None:
    registry = _registry()
    capture = _CapturingMetricExporter()
    reader = PeriodicExportingMetricReader(orx.RedactingMetricExporter(capture, registry))
    provider = MeterProvider(metric_readers=[reader])
    counter = provider.get_meter("kdive.test.redact.metric").create_counter("c")

    counter.add(1, {"detail": f"token={_SECRET}"})
    provider.force_flush()

    rendered = "".join(str(d.to_json()) for d in capture.captured)
    assert capture.captured, "expected an exported metric batch"
    assert _SECRET not in rendered
    assert REDACTION in rendered
