"""Smoke test: the package imports and its logging foundation is wired."""

from __future__ import annotations

import importlib


def test_package_imports() -> None:
    assert importlib.import_module("kdive") is not None


def test_logging_foundation_is_importable() -> None:
    log = importlib.import_module("kdive.log")
    assert callable(log.configure_logging)
    assert callable(log.bind_context)
