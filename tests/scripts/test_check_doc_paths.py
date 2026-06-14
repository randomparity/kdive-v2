# tests/scripts/test_check_doc_paths.py
"""Behavioral tests for scripts/check-doc-paths.sh.

The checker greps justfile / scripts / *.yml / *.md code spans for concrete
docs/<seg>/... references and fails when the target is missing. Illustrative
ellipses (docs/... , docs/…) must NOT be flagged.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-paths.sh"
BASH = shutil.which("bash")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def test_existing_path_passes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide").mkdir()
    (tmp_path / "docs" / "guide" / "index.md").write_text("hi\n")
    (tmp_path / "justfile").write_text("x:\n\techo docs/guide/index.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_path_fails(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("x:\n\techo docs/reports/m2-portability.md\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "docs/reports/m2-portability.md" in result.stderr


def test_illustrative_ellipsis_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("references `docs/<seg>/…` and `docs/...` are fine\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_design_and_archive_markdown_not_scanned(tmp_path: Path) -> None:
    # Design specs narrate path moves and archive is frozen history: their docs/...
    # mentions of missing/old paths must not fail the check.
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "spec.md").write_text("we move docs/specs to docs/design\n")
    (tmp_path / "docs" / "archive" / "old.md").write_text("see docs/plans/m0-implementation.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_paths_inside_code_fences_ignored(tmp_path: Path) -> None:
    # An operational doc may show an example command referencing a path in a code block;
    # that is a sample, not a live reference. \x60 is the backtick byte.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\ncat docs/operating/not-yet.md\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
