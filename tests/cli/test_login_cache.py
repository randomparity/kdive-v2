"""Unit tests for the ``kdivectl login`` token cache (0600 at-rest, missing -> None)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kdive.cli import login


def test_cache_round_trips_and_is_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    login.write_cached_token("abc.def.ghi")
    assert login.read_cached_token() == "abc.def.ghi"
    mode = stat.S_IMODE(os.stat(tmp_path / "token").st_mode)
    assert mode == 0o600


def test_missing_cache_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "absent")
    assert login.read_cached_token() is None


def test_parent_dir_created_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "state" / "kdive" / "token"
    monkeypatch.setattr(login, "_cache_path", lambda: cache)
    login.write_cached_token("t.o.k")
    parent_mode = stat.S_IMODE(os.stat(cache.parent).st_mode)
    assert parent_mode == 0o700


def test_cache_stays_0600_under_widened_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "_cache_path", lambda: tmp_path / "token")
    old = os.umask(0o000)
    try:
        login.write_cached_token("abc.def.ghi")
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(tmp_path / "token").st_mode)
    assert mode == 0o600


def test_rewrite_re_tightens_a_widened_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "token"
    monkeypatch.setattr(login, "_cache_path", lambda: cache)
    login.write_cached_token("first")
    os.chmod(cache, 0o644)
    login.write_cached_token("second")
    assert login.read_cached_token() == "second"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600


def test_empty_cache_file_reads_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "token"
    monkeypatch.setattr(login, "_cache_path", lambda: cache)
    cache.write_text("   \n")
    assert login.read_cached_token() is None


def test_default_cache_path_honours_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    assert login._cache_path() == login.Path("/tmp/xdg-state") / "kdive" / "token"


def test_default_cache_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(login.Path, "home", classmethod(lambda _cls: login.Path("/home/op")))
    assert login._cache_path() == login.Path("/home/op/.local/state/kdive/token")
