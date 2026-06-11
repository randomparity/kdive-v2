"""Entrypoints fail fast on missing required config before doing real work (ADR-0087)."""

from __future__ import annotations

import pytest

from kdive.__main__ import main
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_server_validates_all_required_before_any_io(monkeypatch) -> None:
    for var in (
        "KDIVE_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
    ):
        monkeypatch.delenv(var, raising=False)

    def _boom(*_: object, **__: object) -> None:
        raise AssertionError("create_pool reached before config validation")

    monkeypatch.setattr("kdive.__main__.create_pool", _boom)
    # Stub logging setup: this test asserts validation order, not logging, and the real
    # bootstrap mutates the global logger hierarchy (caplog-fragile for later tests).
    monkeypatch.setattr("kdive.observability.bootstrap_stdout_floor", lambda *a, **k: None)
    monkeypatch.setattr("kdive.observability.init_telemetry", lambda *a, **k: None)
    with pytest.raises(CategorizedError) as ei:
        main(["server"])
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    message = str(ei.value)
    # Aggregated: the old pool-first path failed on DATABASE_URL and never reached OIDC.
    assert "KDIVE_DATABASE_URL" in message
    assert "KDIVE_OIDC_ISSUER" in message


def test_log_level_flag_overrides_env(monkeypatch) -> None:
    # --log-level wins over KDIVE_LOG_LEVEL; install-fixtures is non-runnable so it skips
    # config.validate, and its handler is stubbed so the call needs no database or disk.
    monkeypatch.setenv("KDIVE_LOG_LEVEL", "ERROR")
    captured: dict[str, object] = {}

    def _capture(level: object, **_: object) -> None:
        captured["level"] = level

    monkeypatch.setattr("kdive.observability.bootstrap_stdout_floor", _capture)
    monkeypatch.setattr("kdive.admin.bootstrap.install_fixtures", lambda *a, **k: None)
    main(["--log-level", "DEBUG", "install-fixtures"])
    assert captured["level"] == "DEBUG"


def test_log_level_falls_back_to_registry(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_LOG_LEVEL", "WARNING")
    captured: dict[str, object] = {}

    def _capture(level: object, **_: object) -> None:
        captured["level"] = level

    monkeypatch.setattr("kdive.observability.bootstrap_stdout_floor", _capture)
    monkeypatch.setattr("kdive.admin.bootstrap.install_fixtures", lambda *a, **k: None)
    main(["install-fixtures"])
    assert captured["level"] == "WARNING"
