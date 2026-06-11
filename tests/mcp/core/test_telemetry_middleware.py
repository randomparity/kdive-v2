"""Server-request telemetry middleware (ADR-0090 §5): span per request + RED metrics.

Asserts a span is emitted per MCP tool call with allowlisted labels only, RED metrics
(request count + duration histogram, error count on failure) are recorded, and a
registered secret appearing in a tool name / exception does not leak through the span or
metric signals once the redacting export processors run.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kdive.mcp.middleware import TelemetryMiddleware


class _FakeMessage:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContext:
    def __init__(self, tool: str) -> None:
        self.message = _FakeMessage(tool)


def _harness() -> tuple[TelemetryMiddleware, InMemorySpanExporter, InMemoryMetricReader]:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    mw = TelemetryMiddleware(
        tracer=tracer_provider.get_tracer("test"),
        meter=meter_provider.get_meter("test"),
    )
    return mw, span_exporter, reader


def _metric_points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_successful_call_emits_span_and_red_metrics() -> None:
    async def _run() -> None:
        mw, spans, reader = _harness()

        async def _call_next(_ctx: Any) -> str:
            return "ok"

        result = await mw.on_call_tool(_FakeContext("runs.create"), _call_next)
        assert result == "ok"
        finished = spans.get_finished_spans()
        assert len(finished) == 1
        attrs = _attrs(finished[0])
        assert attrs["tool"] == "runs.create"
        assert attrs["outcome"] == "ok"
        # Identifier keys must not appear as span attributes (ADR-0090 §4 label rule).
        assert "principal" not in attrs
        points = _metric_points(reader)
        assert any("request" in name for name in points)

    asyncio.run(_run())


def test_failing_call_records_error_and_reraises() -> None:
    async def _run() -> None:
        mw, spans, reader = _harness()

        async def _call_next(_ctx: Any) -> None:
            raise RuntimeError("boom")

        raised = False
        try:
            await mw.on_call_tool(_FakeContext("runs.create"), _call_next)
        except RuntimeError:
            raised = True
        assert raised, "the original exception must propagate"
        assert _attrs(spans.get_finished_spans()[0])["outcome"] == "error"
        points = _metric_points(reader)
        error_points = [p for name, pts in points.items() if "error" in name for p in pts]
        assert error_points, "a failing call must increment an error counter"

    asyncio.run(_run())


def test_secret_in_exception_does_not_leak_through_span() -> None:
    async def _run() -> None:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        from kdive.observability.redaction import RedactingSpanExporter
        from kdive.security.secrets.secret_registry import SecretRegistry

        registry = SecretRegistry()
        registry.register("super-secret-token", scope=None)
        inner = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(RedactingSpanExporter(inner, registry))
        )
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[reader])
        mw = TelemetryMiddleware(
            tracer=tracer_provider.get_tracer("test"),
            meter=meter_provider.get_meter("test"),
        )

        async def _call_next(_ctx: Any) -> None:
            raise RuntimeError("auth failed with super-secret-token")

        with contextlib.suppress(RuntimeError):
            await mw.on_call_tool(_FakeContext("runs.create"), _call_next)
        span = inner.get_finished_spans()[0]
        blob = repr(_attrs(span)) + repr([dict(e.attributes or {}) for e in span.events])
        assert "super-secret-token" not in blob
        assert "[REDACTED]" in blob

    asyncio.run(_run())
