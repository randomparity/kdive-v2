"""Shared test fixtures.

The autouse ``reset_config`` fixture clears the config snapshot around every test so a
per-case ``monkeypatch.setenv`` is honored rather than frozen behind a stale snapshot
(ADR-0087's scoped-not-permanent resolution).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import kdive.config as config


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    config.reset()
    yield
    config.reset()
