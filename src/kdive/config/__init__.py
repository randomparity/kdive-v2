"""Central typed configuration registry for the ``KDIVE_*`` contract (ADR-0087).

This package is the single declared source of truth for every ``KDIVE_*`` variable.
Point-of-use code reads through :func:`get` instead of ``os.environ``; startup
:func:`validate` and the generated reference both derive from the same declarations.

Resolution is scoped, not a permanent process-global cache: :func:`load` takes a
snapshot of the environment (at process startup, or per test via the autouse reset
fixture), and :func:`reset` drops it so the next read re-snapshots. This keeps
per-test ``monkeypatch.setenv`` honest.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any

from kdive.config.manifest import SETTING_MODULES
from kdive.config.registry import Registry, Setting

__all__ = [
    "Registry",
    "Setting",
    "all_settings",
    "env_snapshot",
    "get",
    "load",
    "require",
    "reset",
    "validate",
]


def _build_registry() -> Registry:
    settings: list[Setting[Any]] = []
    for path in SETTING_MODULES:
        module = importlib.import_module(path)
        settings.extend(module.SETTINGS)
    return Registry(settings)


_REGISTRY = _build_registry()


def load(env: Mapping[str, str] | None = None) -> None:
    _REGISTRY.load(os.environ if env is None else env)


def reset() -> None:
    _REGISTRY.reset()


def get[T](setting: Setting[T]) -> T | None:
    return _REGISTRY.get(setting)


def require[T](setting: Setting[T]) -> T:
    return _REGISTRY.require(setting)


def validate(process: str) -> None:
    _REGISTRY.validate(process)


def all_settings() -> tuple[Setting, ...]:
    return _REGISTRY.all_settings()


def env_snapshot() -> dict[str, str]:
    return _REGISTRY.env_snapshot()
