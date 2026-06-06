"""Host-free tests for the dcache-demo profile helper (#128, ADR-0056 §3-4)."""

from __future__ import annotations

import pytest

from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from tests.integration import _dcache_demo as demo


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_KERNEL_SRC", "/abs/linux")
    monkeypatch.setenv("KDIVE_TEST_BUILD_CONFIG", "/abs/.config")
    monkeypatch.setenv("KDIVE_GUEST_IMAGE", "/abs/rootfs.qcow2")
    monkeypatch.setenv("KDIVE_DEMO_FIX_PATCH", "/abs/fix.patch")


def test_vulnerable_build_profile_has_no_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    parsed = BuildProfile.parse(demo.demo_build_profile(fixed=False))
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.patch_ref is None
    assert parsed.kernel_source_ref == "/abs/linux"


def test_fixed_build_profile_carries_the_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    parsed = BuildProfile.parse(demo.demo_build_profile(fixed=True))
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.patch_ref == "/abs/fix.patch"


def test_provisioning_profile_is_console_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    raw = demo.demo_provisioning_profile()
    assert ProvisioningProfile.parse(raw) is not None  # parses (does not raise)
    # Console-only invariant: no crashkernel reservation (-> CONSOLE method, ADR-0051 §1),
    # no SSH credential, no destructive opt-in.
    section = raw["provider"]["local-libvirt"]
    assert "crashkernel" not in section
    assert "ssh_credential_ref" not in section
    assert "destructive_ops" not in section


def test_demo_cmdline_carries_the_trigger() -> None:
    assert demo.DEMO_CMDLINE == "console=ttyS0 dhash_entries=1"


def test_preflight_skips_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "KDIVE_KERNEL_SRC",
        "KDIVE_TEST_BUILD_CONFIG",
        "KDIVE_GUEST_IMAGE",
        "KDIVE_DEMO_FIX_PATCH",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(pytest.skip.Exception):
        demo.demo_preflight()
