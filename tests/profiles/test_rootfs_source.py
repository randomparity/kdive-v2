"""Discriminated rootfs source on the libvirt profile (ADR-0048 §3)."""

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


def test_path_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(
        _profile({"kind": "path", "path": "/var/lib/kdive/rootfs/x.qcow2"})
    )
    assert parsed.provider.local_libvirt.rootfs.kind == "path"


def test_catalog_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(
        _profile({"kind": "catalog", "name": "fedora-cloud-base-43-x86_64"})
    )
    rootfs = parsed.provider.local_libvirt.rootfs
    assert rootfs.kind == "catalog"
    assert rootfs.name == "fedora-cloud-base-43-x86_64"


def test_url_kind_requires_sha256() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "url", "url": "https://h/i.qcow2"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_upload_kind_parses() -> None:
    parsed = ProvisioningProfile.parse(_profile({"kind": "upload"}))
    assert parsed.provider.local_libvirt.rootfs.kind == "upload"


def test_unknown_kind_rejected() -> None:
    with pytest.raises(CategorizedError) as e:
        ProvisioningProfile.parse(_profile({"kind": "bogus"}))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
