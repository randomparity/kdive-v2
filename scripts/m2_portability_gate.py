"""Per-PR M2 portability gate (ADR-0076).

Measures the cumulative touched lines (per-commit added+removed — not a net a later
revert can zero out) of every commit since the ``pre-M2`` tag over the
provider-agnostic core (domain/db/jobs/reconciler/services/store/security and the
whole ``mcp`` package including ``mcp/tools/*``), and fails when any file outside the
named allowlist is touched. A second, net ``git diff`` check covers the per-commit
walk's blind spot: a core change introduced only in a merge commit (a conflict
resolution or evil merge), which ``--no-merges`` numstat never sees. The allowlist is
the ADR-0076 set: the ``ResourceKind`` enum value, the one M2 migration, and the
additive ``presign_get`` primitive. Extending it is a deliberate, reviewed decision —
edit this file in the same PR.

Exit codes: 0 gate passes; 1 violations found; 2 the baseline tag is unavailable.
Stdlib-only: CI runs it without a synced environment (``just m2-gate``).
"""

from __future__ import annotations

import subprocess
import sys

BASELINE_TAG = "pre-M2"

CORE_PREFIXES = (
    "src/kdive/domain/",
    "src/kdive/db/",
    "src/kdive/jobs/",
    "src/kdive/reconciler/",
    "src/kdive/services/",
    "src/kdive/store/",
    "src/kdive/security/",
    "src/kdive/mcp/",
)

ALLOWED_FILES = frozenset(
    {
        # ResourceKind.REMOTE_LIBVIRT (ADR-0076 named touch-point).
        "src/kdive/domain/models.py",
        # The one M2 migration: the resources.kind CHECK widen.
        "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
        # The additive presign_get primitive (ADR-0076, ADR-0078).
        "src/kdive/store/objectstore.py",
    }
)


def parse_numstat(out: str) -> dict[str, int]:
    """Aggregate per-file touched lines (added+removed) from ``git log --numstat`` output.

    Binary files render as ``-\\t-\\tpath`` and count as one touched line. Only files
    under the core prefixes are the gate's subject.
    """
    touched: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path.startswith(CORE_PREFIXES):
            continue
        lines = 1 if added == "-" else int(added) + int(removed)
        touched[path] = touched.get(path, 0) + max(lines, 1)
    return touched


def violations(touched: dict[str, int]) -> dict[str, int]:
    """The non-allowlisted core files with any cumulative touch."""
    return {path: count for path, count in touched.items() if path not in ALLOWED_FILES}


def main() -> int:
    tag_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"{BASELINE_TAG}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if tag_check.returncode != 0:
        print(
            f"error: baseline tag {BASELINE_TAG!r} is unavailable; fetch tags/history "
            "(CI: actions/checkout with fetch-depth: 0)",
            file=sys.stderr,
        )
        return 2
    log = subprocess.run(
        [
            "git",
            "log",
            "--numstat",
            "--no-merges",
            "--no-renames",
            "--format=",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    touched = parse_numstat(log.stdout)
    # Union in the net diff: it sees merge-commit-only changes the per-commit walk
    # misses, while the per-commit sum keeps reverted changes counted.
    net = subprocess.run(
        [
            "git",
            "diff",
            "--numstat",
            "--no-renames",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for path, count in parse_numstat(net.stdout).items():
        touched[path] = max(touched.get(path, 0), count)
    allowed = {path: count for path, count in touched.items() if path in ALLOWED_FILES}
    print(f"M2 portability measurement since {BASELINE_TAG} (cumulative touched lines):")
    for path, count in sorted(allowed.items()):
        print(f"  allowlisted  {count:>6}  {path}")
    bad = violations(touched)
    if bad:
        print("\ngate FAILED - provider-specific changes reached the core surface:")
        for path, count in sorted(bad.items()):
            print(f"  VIOLATION    {count:>6}  {path}")
        print(
            "\nRefactor the provider logic out of core (the M2 co-equal goal, "
            "docs/specs/m2-remote-libvirt.md), or - for a deliberate provider-agnostic "
            "core change - extend ALLOWED_FILES in this script in the same PR."
        )
        return 1
    print("gate passed: no core surface touched outside the ADR-0076 allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
