"""Unit tests for the version-info resolver (ADR-0041 decision 5).

Each case mocks one resolution layer at the boundary (the baked import, the `_git`
subprocess wrapper, and `package_version`) and asserts the exact `full_version()` string.
An autouse fixture clears the `version_info` memo between cases so one case's cached
result never masks the next.
"""

from __future__ import annotations

import pytest

from kdive import version
from kdive.version import VersionInfo, full_version, version_info


@pytest.fixture(autouse=True)
def _clear_version_cache():
    version_info.cache_clear()
    yield
    version_info.cache_clear()


def _no_baked(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: None)


def test_baked_release(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", True))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_baked_dev(monkeypatch):
    monkeypatch.setattr(version, "_from_baked", lambda: VersionInfo("0.2.0", "1a2b3c4", False))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_clean_exact_tag_is_release(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0+g1a2b3c4"


def test_live_git_on_tag_but_dirty_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): "v0.2.0",
        ("status", "--porcelain"): " M src/kdive/x.py",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_live_git_untagged_is_dev(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    calls = {
        ("rev-parse", "--short", "HEAD"): "1a2b3c4",
        ("describe", "--tags", "--exact-match", "HEAD"): None,
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_git", lambda *a: calls.get(a))
    assert full_version() == "0.2.0-dev+g1a2b3c4"


def test_unknown_no_baked_no_git(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setattr(version, "package_version", lambda: "0.2.0")
    monkeypatch.setattr(version, "_git", lambda *a: None)
    assert full_version() == "0.2.0-dev"
