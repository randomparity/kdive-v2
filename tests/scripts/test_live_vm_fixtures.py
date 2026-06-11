"""Hygiene tests for the surviving live-VM fixture scripts (#26).

The rootfs/guest-image builder moved to the in-process `RootfsBuildPlane` (M2.4/2,
`python -m kdive build-rootfs`); the remaining fixture script checks out the pinned kernel
source tree the gated walking-skeleton test consumes. This test asserts it parses (`bash -n`),
declares strict mode (`set -euo pipefail`), and is idempotent on a pre-existing destination (a
second invocation against an existing checkout is a no-op exit 0) — the contract the test's
preflight relies on. It never runs the real clone (no network), so it stays in the non-gated
suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_FETCH = _SCRIPTS_DIR / "fetch-kernel-tree.sh"
_BASH = shutil.which("bash")


def test_fetch_parses() -> None:
    """The script is syntactically valid (`bash -n`)."""
    assert _BASH is not None, "bash is required"
    result = subprocess.run([_BASH, "-n", str(_FETCH)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_fetch_declares_strict_mode() -> None:
    """The script declares `set -euo pipefail` (fail-fast on error/unset/pipe)."""
    assert "set -euo pipefail" in _FETCH.read_text()


def test_fetch_is_idempotent_on_existing_tree(tmp_path: Path) -> None:
    """An existing checkout is left in place: the script exits 0 without invoking git."""
    assert _BASH is not None, "bash is required"
    dest = tmp_path / "linux"
    (dest / ".git").mkdir(parents=True)  # look like an existing clone
    result = subprocess.run(
        [_BASH, str(_FETCH), str(dest)],
        env={"PATH": ""},  # empty PATH: a non-idempotent path would fail on the missing git
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "idempotent" in result.stderr
