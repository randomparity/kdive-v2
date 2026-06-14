"""Behavioral tests for deploy/remote-libvirt-guest-helpers/kdive-capture-vmcore.

These pin the ``inspect`` reply contract the worker's ``_parse_inspect`` consumes
(``src/kdive/providers/remote_libvirt/retrieve/kdump_capture.py``): the five JSON keys,
``sha256`` as base64, and — the regression guarded here — that an **absent or empty dump
directory** is a clean ``present=false`` exit-0 reply (which the worker maps to the retryable
READINESS_FAILURE), never a non-zero abort under ``set -euo pipefail`` (which would wrongly
surface as INFRASTRUCTURE_FAILURE).

The helper uses ``find``/``stat``/``openssl``/``dmesg`` only; ``KDIVE_CRASH_DIR`` redirects the
dump-directory scan so the test needs no real kdump. ``dmesg`` may print nothing as an
unprivileged user — that is fine; the test does not assert on the dmesg payload.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "remote-libvirt-guest-helpers"
    / "kdive-capture-vmcore"
)
BASH = shutil.which("bash")

_INSPECT_KEYS = {"present", "sha256", "size_bytes", "build_id", "dmesg_b64"}


def _inspect(crash_dir: Path) -> tuple[int, dict[str, object]]:
    """Run ``inspect`` with the dump scan redirected to ``crash_dir``; return (exit, payload)."""
    assert BASH is not None, "bash is required to run the helper"
    proc = subprocess.run(
        [BASH, str(HELPER), "inspect"],
        env={"PATH": "/usr/bin:/bin", "KDIVE_CRASH_DIR": str(crash_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_absent_dump_dir_is_clean_present_false(tmp_path: Path) -> None:
    """An absent /var/crash must not abort under set -e/pipefail (the reviewed regression)."""
    code, payload = _inspect(tmp_path / "does-not-exist")
    assert code == 0
    assert payload["present"] is False
    assert payload["size_bytes"] == 0
    assert set(payload) == _INSPECT_KEYS


def test_empty_dump_dir_is_clean_present_false(tmp_path: Path) -> None:
    """An existing-but-empty dump directory is also a clean present=false."""
    code, payload = _inspect(tmp_path)
    assert code == 0
    assert payload["present"] is False
    assert set(payload) == _INSPECT_KEYS


def test_present_core_reports_base64_sha_and_size(tmp_path: Path) -> None:
    """A core under <dir>/*/vmcore yields present=true with a base64 sha256 and its byte size."""
    body = b"\x7fELF" + b"kdive-test-core" * 64
    core = tmp_path / "127.0.0.1-2026-06-13-00:00:00" / "vmcore"
    core.parent.mkdir(parents=True)
    core.write_bytes(body)

    code, payload = _inspect(tmp_path)

    assert code == 0
    assert payload["present"] is True
    assert payload["size_bytes"] == len(body)
    assert set(payload) == _INSPECT_KEYS
    # sha256 is the base64-encoded raw digest the worker signs into the presigned PUT.
    decoded = base64.b64decode(str(payload["sha256"]))
    assert len(decoded) == 32


@pytest.mark.parametrize("subcommand", ["", "bogus"])
def test_unknown_subcommand_exits_non_zero(subcommand: str) -> None:
    """A malformed invocation is a real error (non-zero), distinct from a clean no-core reply."""
    assert BASH is not None
    argv = [BASH, str(HELPER)] + ([subcommand] if subcommand else [])
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode != 0


def test_upload_requires_url() -> None:
    """upload without --url is a malformed invocation (non-zero), not a silent success."""
    assert BASH is not None
    proc = subprocess.run(
        [BASH, str(HELPER), "upload"], capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0
