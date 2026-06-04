"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy

from kdive.profiles.provisioning import BootMethod, ProvisioningProfile

_VALID: dict[str, object] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            "crashkernel": "256M",
        }
    },
}


def _valid() -> dict[str, object]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def test_valid_libvirt_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert profile.schema_version == 1
    assert profile.arch == "x86_64"
    assert profile.vcpu == 4
    assert profile.memory_mb == 4096
    assert profile.disk_gb == 20
    assert profile.boot_method is BootMethod.DIRECT_KERNEL
    assert profile.kernel_source_ref.startswith("git+https://")
    assert profile.provider.local_libvirt.domain_xml_params == {"machine": "pc-q35-9.0"}
    assert profile.provider.local_libvirt.rootfs_image_ref.startswith("oci://")


def test_crashkernel_is_present() -> None:
    # The crashkernel reservation is the kdump prerequisite (acceptance criterion).
    profile = ProvisioningProfile.parse(_valid())

    assert profile.provider.local_libvirt.crashkernel == "256M"
