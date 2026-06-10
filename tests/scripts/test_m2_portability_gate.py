"""M2 portability gate: cumulative core-touch measurement vs the pre-M2 tag."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.m2_portability_gate import (
    ALLOWED_FILES,
    parse_numstat,
    render_report,
    violations,
)


def test_render_report_lists_allowlisted_and_flags_violations() -> None:
    md = render_report(
        {
            "src/kdive/domain/models.py": 4,
            "src/kdive/services/resources/discovery.py": 7,
        }
    )
    assert "# M2 portability report" in md
    assert "src/kdive/domain/models.py" in md
    assert "allowlist" in md.lower()
    # a non-allowlisted core file renders under the Violations section + a fail verdict
    assert "## Violations" in md and "src/kdive/services/resources/discovery.py" in md
    assert "gate FAILED" in md


def test_render_report_passes_when_only_allowlisted() -> None:
    md = render_report({"src/kdive/domain/models.py": 4})
    assert "gate passed" in md
    assert "## Violations" not in md


def test_parse_numstat_aggregates_per_file_across_commits() -> None:
    out = (
        "3\t1\tsrc/kdive/store/objectstore.py\n"
        "\n"
        "2\t2\tsrc/kdive/store/objectstore.py\n"
        "5\t0\tsrc/kdive/providers/remote_libvirt/config.py\n"
    )
    touched = parse_numstat(out)
    # Cumulative (per-commit sum), not net — a later revert cannot zero it out.
    assert touched["src/kdive/store/objectstore.py"] == 8
    # Non-core paths are not the gate's subject.
    assert "src/kdive/providers/remote_libvirt/config.py" not in touched


def test_parse_numstat_counts_binary_files_as_touched() -> None:
    touched = parse_numstat("-\t-\tsrc/kdive/db/schema/blob.bin\n")
    assert touched["src/kdive/db/schema/blob.bin"] == 1


def test_violations_excludes_allowlisted_files() -> None:
    touched = {
        "src/kdive/store/objectstore.py": 12,
        "src/kdive/services/resources/discovery.py": 4,
    }
    assert violations(touched) == {"src/kdive/services/resources/discovery.py": 4}


def test_allowlist_is_exactly_the_named_touch_points() -> None:
    # The ADR-0076 set plus the ADR-0085 drgn-live routing touch; extending it is a
    # deliberate, reviewed decision.
    assert (
        frozenset(
            {
                "src/kdive/domain/models.py",
                "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
                "src/kdive/store/objectstore.py",
                "src/kdive/mcp/tools/debug/sessions.py",
                "src/kdive/mcp/tools/debug/introspect.py",
            }
        )
        == ALLOWED_FILES
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
        },
    )


def _write_and_commit(repo: Path, rel: str, content: str, message: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def gate_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_and_commit(repo, "src/kdive/services/svc.py", "x = 1\n", "base")
    _git(repo, "tag", "pre-M2")
    return repo


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[2] / "scripts" / "m2_portability_gate.py"
    return subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)


def test_gate_fails_on_non_allowlisted_core_touch(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 2\n", "leak")
    result = _run_gate(gate_repo)
    assert result.returncode == 1
    assert "src/kdive/services/svc.py" in result.stdout


def test_gate_passes_on_allowlisted_and_provider_touches(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/store/objectstore.py", "def presign_get(): ...\n", "ok")
    _write_and_commit(gate_repo, "src/kdive/providers/remote_libvirt/x.py", "y = 1\n", "provider")
    result = _run_gate(gate_repo)
    assert result.returncode == 0
    assert "src/kdive/store/objectstore.py" in result.stdout  # measurement is reported


def test_gate_counts_reverted_changes(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 2\n", "leak")
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 1\n", "revert")
    result = _run_gate(gate_repo)
    assert result.returncode == 1  # cumulative, not net


def test_gate_errors_usefully_without_the_tag(tmp_path: Path) -> None:
    repo = tmp_path / "untagged"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_and_commit(repo, "README.md", "hi\n", "base")
    result = _run_gate(repo)
    assert result.returncode == 2
    assert "pre-M2" in result.stderr


def test_gate_catches_core_change_introduced_only_in_a_merge_commit(gate_repo: Path) -> None:
    # A conflict resolution (or evil merge) lands only in the merge commit, which
    # per-commit numstat (--no-merges) never sees; the net-diff union must catch it.
    _git(gate_repo, "checkout", "-b", "feature")
    _write_and_commit(gate_repo, "docs/feature.md", "f\n", "feature work")
    _git(gate_repo, "checkout", "main")
    # An "evil merge": the merge commit itself edits a core file.
    _git(gate_repo, "merge", "--no-ff", "--no-commit", "feature")
    (gate_repo / "src/kdive/services/svc.py").write_text("x = 99\n")
    _git(gate_repo, "add", "-A")
    _git(gate_repo, "commit", "-m", "merge feature")
    result = _run_gate(gate_repo)
    assert result.returncode == 1
    assert "src/kdive/services/svc.py" in result.stdout
