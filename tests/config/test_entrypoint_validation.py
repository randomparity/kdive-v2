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
    with pytest.raises(CategorizedError) as ei:
        main(["server"])
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    message = str(ei.value)
    # Aggregated: the old pool-first path failed on DATABASE_URL and never reached OIDC.
    assert "KDIVE_DATABASE_URL" in message
    assert "KDIVE_OIDC_ISSUER" in message


def test_log_level_flag_overrides_env(monkeypatch) -> None:
    # --log-level wins over KDIVE_LOG_LEVEL; a non-runnable command skips validate so the
    # call returns without needing a database.
    monkeypatch.setenv("KDIVE_LOG_LEVEL", "ERROR")
    captured: dict[str, object] = {}

    def _capture(level: object, **_: object) -> None:
        captured["level"] = level

    monkeypatch.setattr("kdive.__main__.configure_logging", _capture)
    main(["--log-level", "DEBUG", "print-local-env"])
    assert captured["level"] == "DEBUG"


def test_log_level_falls_back_to_registry(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_LOG_LEVEL", "WARNING")
    captured: dict[str, object] = {}

    def _capture(level: object, **_: object) -> None:
        captured["level"] = level

    monkeypatch.setattr("kdive.__main__.configure_logging", _capture)
    main(["print-local-env"])
    assert captured["level"] == "WARNING"
