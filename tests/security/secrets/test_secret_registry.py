"""Tests for the process-scoped secret registry (ADR-0027, refines ADR-0012)."""

from __future__ import annotations

from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry


def test_register_then_snapshot_contains_value() -> None:
    registry = SecretRegistry()
    registry.register("hunter2", scope=None)
    assert "hunter2" in registry.snapshot()


def test_empty_and_none_register_are_noops() -> None:
    registry = SecretRegistry()
    before_version = registry.version()
    registry.register("", scope=None)
    registry.register(None, scope=None)
    assert registry.snapshot() == frozenset()
    assert registry.version() == before_version


def test_global_scope_survives_release() -> None:
    registry = SecretRegistry()
    registry.register("global-secret", scope=None)
    registry.release(None)
    assert "global-secret" in registry.snapshot()


def test_bounded_scope_evicted_on_release() -> None:
    registry = SecretRegistry()
    scope = object()
    registry.register("scoped-secret", scope=scope)
    assert "scoped-secret" in registry.snapshot()
    registry.release(scope)
    assert "scoped-secret" not in registry.snapshot()


def test_refcount_keeps_value_until_last_owner_releases() -> None:
    registry = SecretRegistry()
    scope_a = object()
    scope_b = object()
    registry.register("shared", scope=scope_a)
    registry.register("shared", scope=scope_b)
    registry.release(scope_a)
    assert "shared" in registry.snapshot()
    registry.release(scope_b)
    assert "shared" not in registry.snapshot()


def test_version_is_monotonic_on_change() -> None:
    registry = SecretRegistry()
    v0 = registry.version()
    registry.register("a", scope=None)
    v1 = registry.version()
    scope = object()
    registry.register("b", scope=scope)
    v2 = registry.version()
    registry.release(scope)
    v3 = registry.version()
    assert v0 < v1 < v2 < v3


def test_release_of_unknown_scope_does_not_bump_version() -> None:
    registry = SecretRegistry()
    registry.register("a", scope=None)
    before = registry.version()
    registry.release(object())
    assert registry.version() == before


def test_process_registry_is_a_secret_registry() -> None:
    assert isinstance(PROCESS_SECRET_REGISTRY, SecretRegistry)
