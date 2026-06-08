"""Tests for value/pattern redaction and the logging filter (ADR-0027)."""

from __future__ import annotations

import logging

from kdive.security.secrets.redaction import (
    REDACTION,
    Redactor,
    SecretRedactionFilter,
    redact_url_credentials,
)
from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry


def test_redact_url_credentials_strips_userinfo() -> None:
    url = "https://user:pass@host/path"  # pragma: allowlist secret
    assert redact_url_credentials(url) == "https://host/path"


def test_redact_url_credentials_strips_schemeless_userinfo() -> None:
    assert redact_url_credentials("user:pass@host/path") == "host/path"


def test_redact_url_credentials_preserves_clean_url_with_colon_at_in_path() -> None:
    url = "https://host/a:b@c"
    assert redact_url_credentials(url) == url


def test_redact_url_credentials_preserves_port() -> None:
    assert redact_url_credentials("https://u:p@host:5432/db") == "https://host:5432/db"


def test_redactor_masks_value_with_regex_metacharacters() -> None:
    redactor = Redactor(["a.b*c+(d)"])
    assert redactor.redact_text("prefix a.b*c+(d) suffix") == f"prefix {REDACTION} suffix"


def test_redactor_masks_key_value_pairs() -> None:
    redactor = Redactor()
    assert REDACTION in redactor.redact_text("password=hunter2")
    assert REDACTION in redactor.redact_text("token: abc123")


def test_redactor_recurses_into_nested_structures() -> None:
    redactor = Redactor(["sekret"])
    result = redactor.redact_value({"outer": ["sekret", ("sekret",)]})
    assert result == {"outer": [REDACTION, (REDACTION,)]}


def test_redactor_masks_sensitive_path_mapping() -> None:
    redactor = Redactor()
    result = redactor.redact_value({"sensitive": True, "path": "/secret/key"})
    assert result["path"] == REDACTION


def test_redactor_seeds_from_process_global_registry() -> None:
    scope = object()
    PROCESS_SECRET_REGISTRY.register("process-global-sentinel-xyz", scope=scope)
    try:
        redactor = Redactor()
        assert redactor.redact_text("leak process-global-sentinel-xyz here") == (
            f"leak {REDACTION} here"
        )
    finally:
        PROCESS_SECRET_REGISTRY.release(scope)


def test_redaction_filter_masks_newly_registered_value() -> None:
    registry = SecretRegistry()
    log_filter = SecretRedactionFilter(registry)
    registry.register("filter-secret", scope=None)
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="saw filter-secret",
        args=(),
        exc_info=None,
    )
    log_filter.filter(record)
    assert "filter-secret" not in record.getMessage()
    assert REDACTION in record.getMessage()


def test_redaction_filter_rebuilds_only_on_version_change() -> None:
    registry = SecretRegistry()
    log_filter = SecretRedactionFilter(registry)

    def _emit(msg: str) -> str:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        log_filter.filter(record)
        return record.getMessage()

    assert _emit("before any-secret") == "before any-secret"
    registry.register("any-secret", scope=None)
    assert REDACTION in _emit("now any-secret leaks")
