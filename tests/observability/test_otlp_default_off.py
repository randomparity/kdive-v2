"""OTLP export is opt-in (ADR-0090 §2): default-off, on only under KDIVE_OTEL_*.

A dead/absent collector must never be the default state that a missing config key
silently opts into; stdout-only is a complete deployment.
"""

from __future__ import annotations

import kdive.config as config
from kdive.observability import facade


def test_otlp_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_OTEL_ENABLED", raising=False)
    config.load({})
    assert facade.otlp_enabled() is False


def test_otlp_on_requires_explicit_truthy_flag(monkeypatch) -> None:
    config.load({"KDIVE_OTEL_ENABLED": "1", "KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4317"})
    assert facade.otlp_enabled() is True


def test_otlp_falsey_values_stay_off(monkeypatch) -> None:
    for value in ("0", "false", "no", ""):
        config.load({"KDIVE_OTEL_ENABLED": value})
        assert facade.otlp_enabled() is False, f"{value!r} must not enable OTLP"


def test_enabled_without_endpoint_fails_fast(monkeypatch) -> None:
    config.load({"KDIVE_OTEL_ENABLED": "1"})
    try:
        facade.require_otlp_endpoint()
    except Exception as exc:  # noqa: BLE001 - asserting a fail-fast on misconfig
        assert "KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected a configuration error when endpoint is unset")
