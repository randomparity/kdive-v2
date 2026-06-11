"""The stdout log exporter preserves the ADR-0014 schema + additive trace fields.

ADR-0090 §2: every ADR-0014 field keeps its name/meaning so existing consumers and
the log tests are unbroken, and `trace_id`/`span_id` are added so an operator on the
stdout path (`kubectl logs`/`journalctl`) can correlate a record to its trace.
"""

from __future__ import annotations

import json
import logging

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider

from kdive.observability.stdout_exporter import StdoutJsonLogExporter

_ADR0014_FIELDS = ("ts", "level", "logger", "msg")


def _emit(logger_name: str, span: bool = False) -> dict:
    lines: list[str] = []
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=lines.append))
    )
    handler = LoggingHandler(logger_provider=log_provider)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if span:
        tracer = TracerProvider().get_tracer("t")
        with tracer.start_as_current_span("s"):
            logger.info("hello in span")
    else:
        logger.info("hello plain")
    log_provider.force_flush()
    return json.loads([line for line in lines if line.strip()][-1])


def test_stdout_carries_adr0014_fields() -> None:
    record = _emit("kdive.test.stdout.fields")
    for field in _ADR0014_FIELDS:
        assert field in record, f"ADR-0014 field {field} missing"
    assert record["msg"] == "hello plain"
    assert record["level"] == "INFO"


def test_stdout_carries_trace_id_under_active_span() -> None:
    record = _emit("kdive.test.stdout.trace", span=True)
    assert "trace_id" in record
    assert "span_id" in record
    assert record["trace_id"], "trace_id should be a non-empty hex string under a span"
    assert int(record["trace_id"], 16) != 0


def test_stdout_trace_id_empty_without_span() -> None:
    record = _emit("kdive.test.stdout.notrace")
    # The field is always present (stable schema) but empty outside a span.
    assert record.get("trace_id", "") == ""
    assert record.get("span_id", "") == ""


def test_format_helper_emits_single_json_object() -> None:
    log_provider = LoggerProvider()
    captured: list[object] = []
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=lambda _: None))
    )
    handler = LoggingHandler(logger_provider=log_provider)
    logger = logging.getLogger("kdive.test.stdout.helper")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    captured.clear()
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(StdoutJsonLogExporter(write=captured.append))
    )
    logger.info("payload")
    log_provider.force_flush()
    rendered = [c for c in captured if isinstance(c, str) and c.strip()][-1]
    parsed = json.loads(rendered)
    assert parsed["msg"] == "payload"
    assert "\n" not in rendered.strip()
