# tests/scripts/test_check_local_libvirt.py
"""Behavioral tests for scripts/check-local-libvirt.sh.

Runtime state is faked via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override,
so the script's pass/fail paths run without a real libvirt host.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-local-libvirt.sh"
BASH = shutil.which("bash")


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_all_healthy_exits_zero(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # virsh: any subcommand succeeds; `net-info default` reports Active: yes.
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()


def test_missing_kvm_node_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(tmp_path / "nope"),
    }
    result = _run(env)
    assert result.returncode == 1
    assert "kvm" in result.stderr.lower()


def test_user_not_in_libvirt_group_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", "exit 0")
    _stub(bindir, "id", "echo kvm wheel")  # no 'libvirt'
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "KDIVE_KVM_NODE": str(kvm)}
    result = _run(env)
    assert result.returncode == 1
    assert "libvirt" in result.stderr.lower()
