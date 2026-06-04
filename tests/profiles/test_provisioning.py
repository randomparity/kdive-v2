"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import BootMethod, ProvisioningProfile

_VALID: dict[str, Any] = {
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


def _valid() -> dict[str, Any]:
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


def _expect_configuration_error(data: dict[str, Any]) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "arch",
        "vcpu",
        "memory_mb",
        "disk_gb",
        "boot_method",
        "kernel_source_ref",
        "provider",
    ],
)
def test_missing_core_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["rootfs_image_ref", "crashkernel"])
def test_missing_libvirt_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data["provider"]["local-libvirt"][field]
    _expect_configuration_error(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


def test_unknown_provider_key_rejected() -> None:
    data = _valid()
    data["provider"]["cloud"] = {}
    _expect_configuration_error(data)


def test_unknown_libvirt_field_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["extra"] = "x"
    _expect_configuration_error(data)


def test_empty_provider_section_rejected() -> None:
    # The local-libvirt section is required (ADR-0024 decision 1): a profile that
    # names no provider cannot be provisioned.
    data = _valid()
    data["provider"] = {}
    _expect_configuration_error(data)


def test_non_mapping_provider_section_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"] = "not-a-mapping"
    _expect_configuration_error(data)


@pytest.mark.parametrize("payload", [None, [], "not-a-mapping", 42])
def test_non_mapping_input_rejected(payload: Any) -> None:
    # parse() guards the boundary against a caller handing it a non-document.
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(payload)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["arch", "kernel_source_ref"])
def test_blank_core_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["rootfs_image_ref", "crashkernel"])
def test_blank_libvirt_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data["provider"]["local-libvirt"][field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize(("field", "value"), [("vcpu", 0), ("memory_mb", -1), ("disk_gb", 0)])
def test_non_positive_int_rejected(field: str, value: int) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["4", True, 2.0])
@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_non_int_value_rejected(field: str, value: object) -> None:
    # strict=True: a malformed externally-authored value must not silently coerce.
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


def test_empty_domain_xml_param_value_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"machine": ""}
    _expect_configuration_error(data)


def test_domain_xml_params_defaults_to_empty_map() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["domain_xml_params"]

    profile = ProvisioningProfile.parse(data)

    assert profile.provider.local_libvirt.domain_xml_params == {}


def test_unknown_boot_method_rejected() -> None:
    data = _valid()
    data["boot_method"] = "iso"
    _expect_configuration_error(data)


def test_unreadable_schema_version_rejected() -> None:
    data = _valid()
    data["schema_version"] = 2
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_non_int_schema_version_rejected(value: object) -> None:
    # A bool/str/float must not coerce to version 1 (consistent with strict ints).
    data = _valid()
    data["schema_version"] = value
    _expect_configuration_error(data)


def test_error_details_do_not_leak_submitted_values() -> None:
    data = _valid()
    data["memory_mb"] = "S3CRET-LOOKING-VALUE"  # wrong type carrying a sentinel

    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)

    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_profile_is_frozen() -> None:
    profile = ProvisioningProfile.parse(_valid())

    with pytest.raises(ValidationError):
        profile.arch = "aarch64"


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # model_validate is not the sanctioned door; it surfaces the raw ValidationError.
    with pytest.raises(ValidationError):
        ProvisioningProfile.model_validate({"schema_version": 1})


def test_public_names_exported_from_package() -> None:
    import kdive.profiles as profiles

    assert profiles.ProvisioningProfile is ProvisioningProfile
    assert profiles.BootMethod is BootMethod
    assert hasattr(profiles, "LibvirtProfile")
    assert hasattr(profiles, "ProviderSection")
