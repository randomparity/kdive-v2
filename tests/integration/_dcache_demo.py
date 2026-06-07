"""Test-only demo-profile helper for the dcache `dhash_entries=1` A/B (#128, ADR-0056 §3-4).

Resolves the demo's build/provisioning profiles from operator env -- the seams G1/G3
established -- so nothing kernel-sized is committed. Imported by the host-free profile test
and the `live_vm` driver. Not shipped product code.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

_KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"
_CONFIG_ENV = "KDIVE_TEST_BUILD_CONFIG"
_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_FIX_PATCH_ENV = "KDIVE_DEMO_FIX_PATCH"

DEMO_CMDLINE = "dhash_entries=1"
"""The pathological debug arg that triggers the dcache OOB read (test-case 05). The platform
injects the required ``console=ttyS0 root=/dev/vda`` and appends this (ADR-0061), so the demo
carries only the trigger."""


def demo_build_profile(*, fixed: bool) -> dict[str, Any]:
    """The server-build profile: `~/src/linux` + the demo `.config`; `fixed` adds the patch."""
    profile: dict[str, Any] = {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_SRC_ENV],
        "config_ref": os.environ[_CONFIG_ENV],
    }
    if fixed:
        profile["patch_ref"] = os.environ[_FIX_PATCH_ENV]
    return profile


def demo_provisioning_profile() -> dict[str, Any]:
    """A console-only provisioning profile (no crashkernel, no SSH, no destructive opt-in)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_SRC_ENV],
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "path", "path": os.environ[_GUEST_IMAGE_ENV]}}
        },
    }


def demo_preflight() -> None:
    """Resolve the four demo env vars or `pytest.skip` with the exact fix (ADR-0035 §4 style)."""
    fixes = {
        _KERNEL_SRC_ENV: "run scripts/live-vm/fetch-kernel-tree.sh (or point at ~/src/linux)",
        _CONFIG_ENV: (
            "generate a .config (CONFIG_CRASH_DUMP + DWARF/BTF) -- see docs/runbooks/dcache-demo.md"
        ),
        _GUEST_IMAGE_ENV: "run scripts/live-vm/build-guest-image.sh",
        _FIX_PATCH_ENV: (
            "export the dcache 7.0.1 fix as a -p1 patch -- see docs/runbooks/dcache-demo.md"
        ),
    }
    for var, fix in fixes.items():
        value = os.environ.get(var)
        if not value or not os.path.exists(value):
            pytest.skip(f"{var} unset or missing; {fix}")
