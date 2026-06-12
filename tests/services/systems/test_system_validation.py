"""Systems profile validation tests."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.provider_components.references import ROOTFS_COMPONENT, ComponentSourceKind
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import (
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

_LOCAL_POLICY = LocalLibvirtProfilePolicy()
_FAULT_POLICY = FaultInjectProfilePolicy()

_VALID_PROFILE: dict[str, Any] = {
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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _profile(rootfs: dict[str, object] | None = None) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID_PROFILE)
    if rootfs is not None:
        data["provider"]["local-libvirt"]["rootfs"] = rootfs
    return ProvisioningProfile.parse(data)


def _capabilities(*accepted_rootfs_sources: ComponentSourceKind) -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={ROOTFS_COMPONENT: frozenset(accepted_rootfs_sources)},
    )


def test_validate_profile_for_provider_accepts_advertised_rootfs_source() -> None:
    validate_profile_for_provider(_profile(), _LOCAL_POLICY, _capabilities("local"))


def test_validate_profile_for_provider_rejects_unsupported_rootfs_source() -> None:
    with pytest.raises(CategorizedError) as exc_info:
        validate_profile_for_provider(_profile(), _LOCAL_POLICY, _capabilities("catalog"))

    error = exc_info.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {
        "provider": "local-libvirt",
        "component_kind": "rootfs",
        "source_kind": "local",
        "accepted_source_kinds": ["catalog"],
    }


def test_validate_profile_for_provider_runs_static_profile_validation_first() -> None:
    profile = _profile()
    data = profile.model_dump(mode="json", by_alias=True)
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"unsupported": "value"}
    invalid_profile = ProvisioningProfile.parse(data)

    with pytest.raises(CategorizedError) as exc_info:
        validate_profile_for_provider(invalid_profile, _LOCAL_POLICY, _capabilities("local"))

    error = exc_info.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {
        "supported": ["machine"],
        "unsupported": ["unsupported"],
    }


def test_validate_rootfs_for_provider_invokes_validator_for_regular_rootfs() -> None:
    calls: list[RootfsSource] = []

    def validate(rootfs: RootfsSource) -> None:
        calls.append(rootfs)

    validate_rootfs_for_provider(_profile(), _LOCAL_POLICY, validate)

    assert [rootfs.kind for rootfs in calls] == ["local"]


def test_validate_rootfs_for_provider_skips_upload_rootfs() -> None:
    def fail_on_call(_: RootfsSource) -> None:
        raise AssertionError("upload-kind rootfs is system-owned and not provider-validated")

    validate_rootfs_for_provider(_profile({"kind": "upload"}), _LOCAL_POLICY, fail_on_call)
    validate_profile_for_provider(_profile({"kind": "upload"}), _LOCAL_POLICY, _capabilities())


def test_validate_rootfs_for_provider_propagates_validator_error() -> None:
    def reject(_: RootfsSource) -> None:
        raise CategorizedError(
            "rootfs path is outside allowed roots",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"path": "/tmp/rootfs.qcow2"},
        )

    with pytest.raises(CategorizedError) as exc_info:
        validate_rootfs_for_provider(_profile(), _LOCAL_POLICY, reject)

    assert exc_info.value.details == {"path": "/tmp/rootfs.qcow2"}


def test_validate_rootfs_for_provider_skips_providers_without_rootfs() -> None:
    data = copy.deepcopy(_VALID_PROFILE)
    data["provider"] = {"fault-inject": {"capture_method": "console"}}
    profile = ProvisioningProfile.parse(data)

    def fail_on_call(_: RootfsSource) -> None:
        pytest.fail("fault-inject profiles do not expose a provider rootfs")

    validate_rootfs_for_provider(profile, _FAULT_POLICY, fail_on_call)
    validate_profile_for_provider(profile, _FAULT_POLICY, _capabilities())
