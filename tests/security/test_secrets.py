"""Tests for the by-reference secret backend (ADR-0027 §5-6, ADR-0012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.security.paths import PathSafetyError
from kdive.security.redaction import REDACTION, Redactor
from kdive.security.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry
from kdive.security.secrets import FileRefBackend


def _write(root: Path, name: str, content: str) -> Path:
    target = root / name
    target.write_text(content, encoding="utf-8")
    return target


def test_resolve_returns_file_content(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "key", "hunter2\n")
    backend = FileRefBackend(tmp_path, registry)
    assert backend.resolve(str(tmp_path / "key")) == "hunter2"


def test_resolved_value_is_masked_by_redactor(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "key", "s3cr3t-value\n")
    backend = FileRefBackend(tmp_path, registry)
    value = backend.resolve(str(tmp_path / "key"))
    redactor = Redactor(list(registry.snapshot()))
    assert redactor.redact_text(f"output {value} here") == f"output {REDACTION} here"


def test_register_before_return_post_condition(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "key", "ordered-secret\n")
    backend = FileRefBackend(tmp_path, registry)
    value = backend.resolve(str(tmp_path / "key"))
    assert value in registry.snapshot()
    redactor = Redactor(list(registry.snapshot()))
    assert REDACTION in redactor.redact_text(f"saw {value}")


def test_relative_escape_rejected_reads_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path, "outside-secret", "leak\n")
    registry = SecretRegistry()
    backend = FileRefBackend(root, registry)
    with pytest.raises(PathSafetyError):
        backend.resolve(str(root / ".." / "outside-secret"))
    assert registry.snapshot() == frozenset()


def test_absolute_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = _write(tmp_path, "outside-secret", "leak\n")
    registry = SecretRegistry()
    backend = FileRefBackend(root, registry)
    with pytest.raises(PathSafetyError):
        backend.resolve(str(outside))
    assert registry.snapshot() == frozenset()


def test_nonexistent_file_rejected(tmp_path: Path) -> None:
    registry = SecretRegistry()
    backend = FileRefBackend(tmp_path, registry)
    with pytest.raises(PathSafetyError):
        backend.resolve(str(tmp_path / "missing"))
    assert registry.snapshot() == frozenset()


def test_empty_file_returns_empty_and_registers_nothing(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "empty", "")
    _write(tmp_path, "newline-only", "\n")
    backend = FileRefBackend(tmp_path, registry)
    assert backend.resolve(str(tmp_path / "empty")) == ""
    assert backend.resolve(str(tmp_path / "newline-only")) == ""
    assert registry.snapshot() == frozenset()


def test_terminator_strip(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "lf", "secret\n")
    _write(tmp_path, "crlf", "secret\r\n")
    _write(tmp_path, "trailing-space", "secret \n")
    backend = FileRefBackend(tmp_path, registry)
    assert backend.resolve(str(tmp_path / "lf")) == "secret"
    assert backend.resolve(str(tmp_path / "crlf")) == "secret"
    assert backend.resolve(str(tmp_path / "trailing-space")) == "secret "


def test_oversized_file_rejected(tmp_path: Path) -> None:
    registry = SecretRegistry()
    _write(tmp_path, "huge", "x" * (64 * 1024 + 1))
    backend = FileRefBackend(tmp_path, registry)
    with pytest.raises(PathSafetyError):
        backend.resolve(str(tmp_path / "huge"))
    assert registry.snapshot() == frozenset()


def test_default_backend_registers_into_process_global(tmp_path: Path) -> None:
    scope = object()
    _write(tmp_path, "key", "default-global-value\n")
    backend = FileRefBackend(tmp_path, scope=scope)
    try:
        value = backend.resolve(str(tmp_path / "key"))
        assert value in PROCESS_SECRET_REGISTRY.snapshot()
    finally:
        PROCESS_SECRET_REGISTRY.release(scope)
    assert value not in PROCESS_SECRET_REGISTRY.snapshot()


def test_scope_is_plumbed_through(tmp_path: Path) -> None:
    registry = SecretRegistry()
    scope = object()
    _write(tmp_path, "key", "scoped-value\n")
    backend = FileRefBackend(tmp_path, registry, scope=scope)
    value = backend.resolve(str(tmp_path / "key"))
    assert value in registry.snapshot()
    registry.release(scope)
    assert value not in registry.snapshot()
