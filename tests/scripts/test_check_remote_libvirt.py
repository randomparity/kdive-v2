# tests/scripts/test_check_remote_libvirt.py
"""Behavioral tests for scripts/check-remote-libvirt.sh.

ssh / virsh are PATH-stubbed; TLS PKI and staged guest-helper checks use directory
overrides so the pre-deploy (no provisioned guest) path runs without real infra.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-remote-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def _healthy_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "ssh", "exit 0")
    _stub(bindir, "virsh", "exit 0")
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "clientcert.pem").write_text("x")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "kdive-drgn").write_text("x")
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_REMOTE_PKI_DIR": str(pki),
        "KDIVE_GUEST_HELPERS_DIR": str(helpers),
    }


def test_healthy_remote_exits_zero(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 0, result.stderr


def test_unreachable_ssh_fails(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    _stub(Path(env["PATH"]), "ssh", "exit 255")
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 1
    assert "ssh" in result.stderr.lower()


def test_missing_pki_fails(tmp_path: Path) -> None:
    env = _healthy_env(tmp_path)
    env["KDIVE_REMOTE_PKI_DIR"] = str(tmp_path / "absent")
    result = _run(["host.example", "kdive", "qemu+tls://host.example/system"], env)
    assert result.returncode == 1
    assert "pki" in result.stderr.lower()
