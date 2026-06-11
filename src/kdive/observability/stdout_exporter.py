"""Stdout JSON log exporter preserving the ADR-0014 schema (ADR-0090 §2, §7).

The stdout path is the always-on log floor for every deployment shape (kubelet under
K8s, journald under systemd, the terminal in a bare venv). It reproduces ADR-0014's
JSON field schema — ``ts``, ``level``, ``logger``, ``msg``, the ``bind_context``
domain fields, ``exc`` — and adds two *additive* fields, ``trace_id`` and ``span_id``,
so an operator on the stdout path (exactly the path in use when the collector is down)
can correlate a record to its trace. Existing fields keep their name and meaning, so
the ADR-0014 log tests are unbroken.

This module imports the pre-stable ``opentelemetry.sdk._logs`` types and therefore
lives inside the ``kdive/observability`` facade (ADR-0090 §7).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections.abc import Callable, Sequence
from typing import IO, Any

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

from kdive.log import CONTEXT_FIELDS

_CTX_RECORD_ATTR = "_kdive_ctx"
_OTEL_CODE_PREFIX = ("code.", "exception.")


def _hex_or_empty(value: int, width: int) -> str:
    """Render a trace/span id as fixed-width hex, or empty when unset (id ``0``)."""
    return format(value, f"0{width}x") if value else ""


def format_log_record_json(record: ReadableLogRecord) -> str:
    """Render one OTel log record as a single-line ADR-0014 JSON object + trace fields."""
    log_record = record.log_record
    attributes: dict[str, Any] = dict(log_record.attributes or {})
    created = (log_record.timestamp or log_record.observed_timestamp or 0) / 1e9
    payload: dict[str, object] = {
        "ts": _dt.datetime.fromtimestamp(created, tz=_dt.UTC).isoformat(),
        "level": log_record.severity_text or "INFO",
        "logger": record.instrumentation_scope.name if record.instrumentation_scope else "",
        "msg": "" if log_record.body is None else str(log_record.body),
    }
    payload.update(_domain_context(attributes))
    stacktrace = attributes.get("exception.stacktrace")
    if isinstance(stacktrace, str):
        payload["exc"] = stacktrace
    payload["trace_id"] = _hex_or_empty(log_record.trace_id, 32)
    payload["span_id"] = _hex_or_empty(log_record.span_id, 16)
    return json.dumps(payload, default=str)


def _domain_context(attributes: dict[str, Any]) -> dict[str, object]:
    """Flatten the ``bind_context`` fields back into the ADR-0014 top-level schema."""
    bound = attributes.get(_CTX_RECORD_ATTR)
    context: dict[str, object] = {}
    if isinstance(bound, dict):
        context.update({k: v for k, v in bound.items() if k in CONTEXT_FIELDS})
    for key, value in attributes.items():
        if key in CONTEXT_FIELDS and key not in context:
            context[key] = value
    return context


class StdoutJsonLogExporter(LogRecordExporter):
    """A console log exporter that writes the ADR-0014 JSON line per record.

    Args:
        out: The text stream to write to (defaults to ``sys.stderr``, matching the
            ADR-0014 stdlib handler's destination).
        write: An optional sink callable receiving each rendered line; when given it
            replaces ``out`` (used by tests to capture output without a stream).
    """

    def __init__(
        self,
        out: IO[str] | None = None,
        *,
        write: Callable[[str], object] | None = None,
    ) -> None:
        self._out = out if out is not None else sys.stderr
        self._write = write

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        for record in batch:
            line = format_log_record_json(record)
            if self._write is not None:
                self._write(line)
            else:
                self._out.write(line + "\n")
        if self._write is None:
            self._out.flush()
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
