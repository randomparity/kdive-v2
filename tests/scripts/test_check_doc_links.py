# tests/scripts/test_check_doc_links.py
"""Behavioral tests for scripts/check-doc-links.sh.

The checker resolves relative markdown links in tracked *.md files against the
filesystem. Tests build a tiny tree with a good and a broken link and assert the
exit status and that the broken target is named.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-links.sh"
BASH = shutil.which("bash")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolvable_links_pass(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [b](b.md)\n")
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_broken_link_fails_and_names_target(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [gone](missing.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "missing.md" in result.stderr
    assert "a.md" in result.stderr


def test_external_and_anchor_only_links_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[x](https://example.com) [y](#section)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_links_inside_code_fences_ignored(tmp_path: Path) -> None:
    # A doc may show an example markdown link inside a code sample; that is not a real
    # cross-reference and must not be resolved. \x60 is the backtick byte; the three of
    # them form a fence without putting a literal fence marker in this test file.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\nsee [gone](does-not-exist.md)\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_titled_link_resolves(tmp_path: Path) -> None:
    # CommonMark allows a title after the destination; it must not be treated as part
    # of the path.
    (tmp_path / "a.md").write_text('see [b](b.md "the title")\n')
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_design_and_archive_not_link_checked(tmp_path: Path) -> None:
    # Archived history is frozen (its links pointed at the old tree) and design specs are
    # narrative; neither must fail the link gate.
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "archive" / "old.md").write_text("[x](../../adr/gone.md)\n")
    (tmp_path / "docs" / "design" / "spec.md").write_text("[y](does-not-exist.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
