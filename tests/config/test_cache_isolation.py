"""The scoped (resettable) snapshot keeps per-test ``setenv`` honest (ADR-0087)."""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import HTTP_PORT


def test_setenv_is_honored_first(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_HTTP_PORT", "9101")
    config.load()
    assert config.get(HTTP_PORT) == 9101


def test_setenv_is_honored_second(monkeypatch) -> None:
    # Without the reset seam this would still see 9101 from the previous test.
    monkeypatch.setenv("KDIVE_HTTP_PORT", "9202")
    config.load()
    assert config.get(HTTP_PORT) == 9202


def test_reset_fixture_relazy_loads_without_explicit_load(monkeypatch) -> None:
    # No explicit config.load(): the autouse reset fixture cleared the snapshot, so the
    # lazy read re-snapshots from os.environ and sees this test's value.
    monkeypatch.setenv("KDIVE_HTTP_PORT", "9303")
    assert config.get(HTTP_PORT) == 9303
