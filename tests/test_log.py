"""Behavioral tests for the structured-logging foundation (ADR-0014)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid

import pytest

from kdive import log as klog
from kdive.security.secrets.redaction import REDACTION, SecretRedactionFilter
from kdive.security.secrets.secret_registry import SecretRegistry


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """Build an isolated logger wired with the JSON formatter + context filter.

    Returns the logger and the stream its single handler writes to, so a test can
    read back exactly what would be emitted without touching the root logger.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(klog.JsonFormatter())
    handler.addFilter(klog.ContextFilter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def _last_record(stream: io.StringIO) -> dict:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one emitted log line"
    return json.loads(lines[-1])


def test_emits_single_json_object_with_core_fields() -> None:
    logger, stream = _capture_logger("kdive.test.core")
    logger.info("hello")
    record = _last_record(stream)
    assert record["msg"] == "hello"
    assert record["level"] == "INFO"
    assert record["logger"] == "kdive.test.core"
    assert "ts" in record


def test_message_args_are_rendered() -> None:
    logger, stream = _capture_logger("kdive.test.args")
    logger.info("provisioned %s in %dms", "system-7", 1200)
    record = _last_record(stream)
    assert record["msg"] == "provisioned system-7 in 1200ms"


def test_bound_context_appears_on_record() -> None:
    logger, stream = _capture_logger("kdive.test.bound")
    with klog.bind_context(request_id="req-1", principal="alice", transition="ready"):
        logger.info("transition")
    record = _last_record(stream)
    assert record["request_id"] == "req-1"
    assert record["principal"] == "alice"
    assert record["transition"] == "ready"


def test_unbound_context_fields_are_absent() -> None:
    logger, stream = _capture_logger("kdive.test.unbound")
    logger.info("no context")
    record = _last_record(stream)
    for field in ("request_id", "job_id", "principal", "object_id", "transition"):
        assert field not in record


def test_context_is_reset_after_block() -> None:
    logger, stream = _capture_logger("kdive.test.reset")
    with klog.bind_context(request_id="req-1"):
        logger.info("inside")
    logger.info("outside")
    record = _last_record(stream)
    assert "request_id" not in record


def test_nested_bind_overrides_then_restores() -> None:
    logger, stream = _capture_logger("kdive.test.nested")
    with klog.bind_context(request_id="outer", principal="alice"):
        with klog.bind_context(request_id="inner"):
            logger.info("inner line")
        inner = _last_record(stream)
        logger.info("outer line")
        outer = _last_record(stream)
    assert inner["request_id"] == "inner"
    assert inner["principal"] == "alice"
    assert outer["request_id"] == "outer"


def test_unknown_context_field_is_rejected() -> None:
    with (
        pytest.raises(ValueError, match="unknown log context field"),
        klog.bind_context(not_a_field="x"),
    ):
        pass


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        registry = SecretRegistry()
        klog.configure_logging(secret_registry=registry)
        count_after_first = len(root.handlers)
        klog.configure_logging(secret_registry=registry)
        count_after_second = len(root.handlers)
        assert count_after_first == count_after_second, "second call must not add a handler"
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, klog.JsonFormatter)]
        assert len(json_handlers) == 1
        handler = json_handlers[0]
        assert any(isinstance(f, klog.ContextFilter) for f in handler.filters)
        assert any(isinstance(f, SecretRedactionFilter) for f in handler.filters)
    finally:
        root.handlers = before


def test_configure_logging_updates_redaction_registry() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        first = SecretRegistry()
        second = SecretRegistry()
        klog.configure_logging(secret_registry=first)
        klog.configure_logging(secret_registry=second)
        handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        filters = [
            log_filter
            for handler in handlers
            for log_filter in handler.filters
            if isinstance(log_filter, SecretRedactionFilter)
        ]
        assert len(filters) == 1
        first.register("old-secret", scope=None)
        second.register("new-secret", scope=None)
        record = logging.LogRecord(
            "kdive.test.redaction",
            logging.INFO,
            __file__,
            0,
            "old-secret new-secret",
            None,
            None,
        )
        filters[0].filter(record)
        assert "old-secret" in record.getMessage()
        assert record.getMessage() == f"old-secret {REDACTION}"
    finally:
        root.handlers = before


def test_exception_traceback_is_captured() -> None:
    logger, stream = _capture_logger("kdive.test.exc")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("operation failed")
    record = _last_record(stream)
    assert record["msg"] == "operation failed"
    assert "RuntimeError: boom" in record["exc"]


def test_non_serializable_context_value_is_coerced_not_crashed() -> None:
    formatter = klog.JsonFormatter()
    record = logging.LogRecord("kdive.test.serial", logging.INFO, __file__, 0, "msg", None, None)
    object_id = uuid.uuid4()
    setattr(record, klog._CTX_RECORD_ATTR, {"object_id": object_id})
    payload = json.loads(formatter.format(record))
    assert payload["object_id"] == str(object_id)


def test_context_is_isolated_between_concurrent_tasks() -> None:
    logger, stream = _capture_logger("kdive.test.async")

    async def bound() -> None:
        with klog.bind_context(request_id="task-A"):
            await asyncio.sleep(0)
            logger.info("from-A")

    async def unbound() -> None:
        await asyncio.sleep(0)
        logger.info("from-B")

    async def main() -> None:
        await asyncio.gather(bound(), unbound())

    asyncio.run(main())

    records = {
        json.loads(line)["msg"]: json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    }
    assert records["from-A"]["request_id"] == "task-A"
    assert "request_id" not in records["from-B"]
