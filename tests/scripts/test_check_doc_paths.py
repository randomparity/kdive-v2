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


def test_substring_docs_not_matched(tmp_path: Path) -> None:
    # The docs/ token is anchored on a left word boundary, so a substring inside a larger
    # word (mkdocs/, subdocs/) is not mistaken for a docs/ reference.
    (tmp_path / "a.md").write_text("see mkdocs/foo and subdocs/bar for the tool\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_changelog_not_scanned(tmp_path: Path) -> None:
    # CHANGELOG.md is git-cliff-generated and reproduces commit subjects verbatim, which
    # may contain docs/-prefixed recipe-name tokens (e.g. "docs/docs-check") that are not
    # filesystem paths. The generated changelog must not be policed for path existence.
    (tmp_path / "CHANGELOG.md").write_text("- Add just docs/docs-check and gate ci\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_vendored_tool_dirs_not_scanned(tmp_path: Path) -> None:
    # Vendored agent-tooling config (.claude/, .agents/, .codex/) is not project docs;
    # its illustrative docs/... example strings must not be policed.
    skill = tmp_path / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("e.g. your overlay doc `docs/nope.md`\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
