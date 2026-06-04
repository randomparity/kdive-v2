"""Tests for the scoped path-safety primitive (ADR-0027 §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.security.paths import PathSafetyError, confine_to_root


def test_file_under_root_resolves(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("x")
    assert confine_to_root(target, allowed_root=tmp_path) == target.resolve()


def test_relative_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    escape = root / ".." / "outside"
    with pytest.raises(PathSafetyError):
        confine_to_root(escape, allowed_root=root)


def test_absolute_path_outside_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other" / "secret"
    with pytest.raises(PathSafetyError):
        confine_to_root(outside, allowed_root=root)


def test_symlink_to_existing_outside_file_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("leak")
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(PathSafetyError):
        confine_to_root(link, allowed_root=root)


def test_symlink_to_nonexistent_outside_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    link = root / "dangling"
    link.symlink_to(tmp_path / "nowhere" / "ghost")
    with pytest.raises(PathSafetyError):
        confine_to_root(link, allowed_root=root)


def test_shell_metachar_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        confine_to_root(tmp_path / "a;b", allowed_root=tmp_path)


def test_control_char_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        confine_to_root(Path(f"{tmp_path}/a\x01b"), allowed_root=tmp_path)


def test_not_yet_existing_tail_under_root_admitted(tmp_path: Path) -> None:
    candidate = tmp_path / "subdir" / "future.txt"
    resolved = confine_to_root(candidate, allowed_root=tmp_path)
    assert resolved == candidate.resolve()
