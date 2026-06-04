"""Hygiene tests for the live_vm fixture scripts (#26).

The scripts produce the kernel tree + guest image the gated walking-skeleton test consumes.
These tests assert each script parses (`bash -n`), declares strict mode (`set -euo pipefail`),
and is idempotent on a pre-existing destination (a second invocation against an existing
fixture is a no-op exit 0) — the contract the test's preflight relies on. They never run the
real clone/build (no network, no qemu), so they stay in the non-gated suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "live-vm"
_FETCH = _SCRIPTS_DIR / "fetch-kernel-tree.sh"
_BUILD = _SCRIPTS_DIR / "build-guest-image.sh"
_BASH = shutil.which("bash")


@pytest.mark.parametrize("script", [_FETCH, _BUILD], ids=["fetch-kernel-tree", "build-guest-image"])
def test_script_parses(script: Path) -> None:
    """The script is syntactically valid (`bash -n`)."""
    assert _BASH is not None, "bash is required"
    result = subprocess.run([_BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [_FETCH, _BUILD], ids=["fetch-kernel-tree", "build-guest-image"])
def test_script_declares_strict_mode(script: Path) -> None:
    """The script declares `set -euo pipefail` (fail-fast on error/unset/pipe)."""
    assert "set -euo pipefail" in script.read_text()


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


def test_build_is_idempotent_on_existing_image(tmp_path: Path) -> None:
    """An existing image is left in place: the script exits 0 without invoking qemu-img."""
    assert _BASH is not None, "bash is required"
    dest = tmp_path / "guest.qcow2"
    dest.write_bytes(b"")  # look like an existing image
    result = subprocess.run(
        [_BASH, str(_BUILD), str(dest)],
        env={"PATH": ""},  # empty PATH: a non-idempotent path would fail on missing qemu-img
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "idempotent" in result.stderr
