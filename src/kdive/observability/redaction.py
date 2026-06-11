"""Redaction at the OTel SDK export boundary, across all three signals (ADR-0090 §4).

Adopting OTLP opens two secret-egress paths besides logs: span attributes/events and
metric labels both routinely carry secret-bearing data (a connection URL with an
embedded token, an exception message, a request parameter). The redaction invariant is
therefore enforced on the export boundary of *every* signal — a redacting log-record
processor, a redacting span exporter, and a redacting metric exporter, each running
before its real exporter. The existing :class:`Redactor` logic is reused; only its
placement moves from the stdlib log path to these three SDK hooks.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.view import Aggregation


class _RegistryRedactor:
    """Cache a :class:`Redactor`, rebuilding only when the registry version changes."""

    def __init__(self, registry: SecretRegistry) -> None:
        self._registry = registry
        self._cached_version = -1
        self._redactor = Redactor(registry=registry)

    def current(self) -> Redactor:
        version = self._registry.version()
        if version != self._cached_version:
            self._redactor = Redactor(registry=self._registry)
            self._cached_version = version
        return self._redactor


def _redact_attributes(redactor: Redactor, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {key: redactor.redact_value(value) for key, value in attributes.items()}


class RedactingLogProcessor(LogRecordProcessor):
    """Scrub registered secrets from a log record's body + attributes before export.

    Added as the *first* log-record processor so every downstream exporter (stdout
    JSON and OTLP) serializes the already-redacted record.
    """

    def __init__(self, registry: SecretRegistry) -> None:
        self._redactor = _RegistryRedactor(registry)

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        redactor = self._redactor.current()
        record = log_record.log_record
        if record.body is not None:
            record.body = redactor.redact_value(record.body)
        if record.attributes:
            record.attributes = _redact_attributes(redactor, record.attributes)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class RedactingSpanExporter(SpanExporter):
    """Wrap a span exporter, scrubbing registered secrets from a span before export.

    Both span attributes **and span events** (ADR-0090 §4) are scrubbed: an
    ``exception`` event's ``exception.message``/``exception.stacktrace`` routinely
    carries secret-bearing data, so events must be redacted alongside attributes.
    """

    def __init__(self, inner: SpanExporter, registry: SecretRegistry) -> None:
        self._inner = inner
        self._redactor = _RegistryRedactor(registry)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        redactor = self._redactor.current()
        for span in spans:
            attributes = span.attributes
            if attributes:
                # BoundedAttributes is a mapping; replace the private store in place so
                # the exported ReadableSpan carries the redacted values.
                span._attributes = _redact_attributes(redactor, attributes)  # noqa: SLF001
            for event in span.events:
                if event.attributes:
                    event._attributes = _redact_attributes(  # noqa: SLF001
                        redactor, event.attributes
                    )
        return self._inner.export(spans)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class RedactingMetricExporter(MetricExporter):
    """Wrap a metric exporter, scrubbing registered secrets from data-point labels."""

    def __init__(self, inner: MetricExporter, registry: SecretRegistry) -> None:
        super().__init__(
            preferred_temporality=getattr(inner, "_preferred_temporality", None),
            preferred_aggregation=getattr(inner, "_preferred_aggregation", None),
        )
        self._inner = inner
        self._redactor = _RegistryRedactor(registry)

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs: Any
    ) -> MetricExportResult:
        redactor = self._redactor.current()
        _redact_metrics_data(redactor, metrics_data)
        return self._inner.export(metrics_data, timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float = 10000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30000, **kwargs: Any) -> None:
        self._inner.shutdown(timeout_millis, **kwargs)

    def _preferred_aggregation_fallback(self) -> dict[type, Aggregation]:  # pragma: no cover
        return {}


def _redact_metrics_data(redactor: Redactor, metrics_data: MetricsData) -> None:
    """Rewrite each data point's attributes in place with redacted values.

    Metric data points are frozen dataclasses, so each is replaced by a copy carrying
    redacted attributes; the containing ``data`` object's ``data_points`` list is
    mutated in place (it is a mutable list at the SDK's export boundary).
    """
    for resource_metric in metrics_data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points = getattr(metric.data, "data_points", None)
                if not isinstance(points, list):
                    continue
                points[:] = [
                    dataclasses.replace(
                        point, attributes=_redact_attributes(redactor, point.attributes)
                    )
                    for point in points
                ]
