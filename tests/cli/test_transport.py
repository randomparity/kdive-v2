"""Error-category → CLI exit-code mapping and the token-attach session (ADR-0089)."""

from __future__ import annotations

import pytest

import kdive.config as config
from kdive.cli import transport
from kdive.cli.errors import exit_code_for_category, exit_code_for_envelope
from kdive.config.cli_settings import SERVER_URL, TOKEN


def test_authorization_denied_maps_to_code_3() -> None:
    assert exit_code_for_category("authorization_denied") == 3


def test_configuration_error_maps_to_code_2() -> None:
    assert exit_code_for_category("configuration_error") == 2


def test_not_found_maps_to_code_4() -> None:
    assert exit_code_for_category("not_found") == 4


def test_conflict_maps_to_code_5() -> None:
    assert exit_code_for_category("conflict") == 5


def test_unknown_category_maps_to_generic_1() -> None:
    assert exit_code_for_category("something_else") == 1


def test_empty_category_maps_to_generic_1() -> None:
    assert exit_code_for_category("") == 1


def test_not_found_envelope_maps_to_code_4() -> None:
    # End-to-end: a server not_found failure envelope becomes the reserved exit code 4, making
    # "that id doesn't exist" observable distinctly from "you typed garbage" (exit 2).
    assert exit_code_for_envelope({"error_category": "not_found"}) == 4


def test_success_envelope_maps_to_code_0() -> None:
    assert exit_code_for_envelope({"status": "ok"}) == 0


def test_session_from_env_uses_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN.name, "abc.def.ghi")
    monkeypatch.setenv(SERVER_URL.name, "http://server:9000/mcp")
    config.load()
    session = transport.Session.from_env()
    assert session.token == "abc.def.ghi"
    assert session.url == "http://server:9000/mcp"


def test_session_from_env_falls_back_to_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN.name, raising=False)
    monkeypatch.setattr(transport, "read_cached_token", lambda: "cached.token")
    config.load()
    session = transport.Session.from_env()
    assert session.token == "cached.token"
    assert session.url == SERVER_URL.default


def test_session_from_env_without_token_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN.name, raising=False)
    monkeypatch.setattr(transport, "read_cached_token", lambda: None)
    config.load()
    with pytest.raises(SystemExit) as excinfo:
        transport.Session.from_env()
    assert "KDIVE_TOKEN" in str(excinfo.value)
