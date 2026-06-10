"""Mock-OIDC ``kdivectl login`` flow (gated on the mock issuer, like the live-stack tests).

Marked ``oidc_issuer`` and guarded by ``require_issuer()`` so it runs only when the
mock-oauth2-server is up; otherwise it skips (it never un-gates the integration boundary).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

import kdive.config as config
from kdive.cli import login, transport
from kdive.cli.login import OidcIssuer
from kdive.config.cli_settings import CLI_CLIENT_ID, TOKEN
from tests.integration.live_stack.conftest import require_issuer


@pytest.mark.oidc_issuer
def test_login_mints_platform_admin_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    config.load()
    token = login.login(platform_role="platform_admin")
    assert token
    assert login.read_cached_token() == token
    assert stat.S_IMODE(os.stat(tmp_path / "token").st_mode) == 0o600


@pytest.mark.oidc_issuer
def test_login_without_platform_role_still_mints_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    config.load()
    token = login.login(platform_role=None)
    assert token
    assert login.read_cached_token() == token


@pytest.mark.oidc_issuer
def test_session_picks_up_login_cache_when_token_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    monkeypatch.delenv(TOKEN.name, raising=False)
    config.load()
    token = login.login(platform_role="platform_operator")
    session = transport.Session.from_env()
    assert session.token == token


@pytest.mark.oidc_issuer
def test_login_sets_azp_from_cli_client_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require_issuer()
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    monkeypatch.setenv(CLI_CLIENT_ID.name, "kdivectl")
    config.load()
    captured: dict[str, object] = {}
    real_authorization_code = login._authorization_code

    def _capture(issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
        captured.update(claims)
        return real_authorization_code(issuer, claims)

    monkeypatch.setattr(login, "_authorization_code", _capture)
    login.login(platform_role="platform_admin")
    assert captured["azp"] == "kdivectl"
    assert captured["platform_roles"] == ["platform_admin"]
