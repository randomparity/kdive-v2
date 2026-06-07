"""Discriminated rootfs source on the libvirt profile (ADR-0065)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile


def _profile(rootfs: dict) -> dict:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git#v7.0",
        "provider": {"local-libvirt": {"rootfs": rootfs, "crashkernel": "256M"}},
    }


def test_local_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(
        _profile({"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"})
    )
    assert parsed.provider.local_libvirt.rootfs.kind == "local"


def test_catalog_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(
        _profile(
            {
                "kind": "catalog",
                "provider": "local-libvirt",
                "name": "fedora-kdive-ready-43",
            }
        )
    )
    rootfs = parsed.provider.local_libvirt.rootfs
    assert rootfs.kind == "catalog"
    assert rootfs.name == "fedora-kdive-ready-43"


def test_artifact_kind_requires_uuid() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "artifact", "artifact_id": "not-a-uuid"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_upload_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(_profile({"kind": "upload"}))
    assert parsed.provider.local_libvirt.rootfs.kind == "upload"


def test_unknown_kind_rejected() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "bogus"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
