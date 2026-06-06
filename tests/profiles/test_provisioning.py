"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import BootMethod, ProvisioningProfile, profile_digest

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
            "rootfs": {
                "kind": "path",
                "path": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            },
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
    rootfs = profile.provider.local_libvirt.rootfs
    assert rootfs.kind == "path"
    assert rootfs.path.startswith("oci://")


def test_crashkernel_is_present() -> None:
    # The crashkernel reservation is the kdump prerequisite (acceptance criterion).
    profile = ProvisioningProfile.parse(_valid())

    assert profile.provider.local_libvirt.crashkernel == "256M"


def test_ssh_credential_ref_defaults_to_none() -> None:
    # A profile that opts out of live ssh introspection carries no credential reference.
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.ssh_credential_ref is None


def test_ssh_credential_ref_parses_when_present() -> None:
    # The reference is an opaque, non-empty token into the file-ref secret backend (ADR-0039);
    # it is a reference, never the credential value itself.
    data = _valid()
    data["provider"]["local-libvirt"]["ssh_credential_ref"] = "ssh/guest-key"
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.ssh_credential_ref == "ssh/guest-key"


def test_ssh_credential_ref_rejects_blank() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["ssh_credential_ref"] = "   "
    _expect_configuration_error(data)


def test_profile_digest_is_stable_hex() -> None:
    digest = profile_digest(ProvisioningProfile.parse(_valid()))
    assert len(digest) == 64  # sha256 hex
    assert int(digest, 16) >= 0  # all hex


def test_profile_digest_ignores_input_key_order() -> None:
    # Digest equality must be semantic equality (ADR-0038 dedup correctness): the same
    # profile submitted with a different key order yields the same digest.
    a = _valid()
    reordered = {k: a[k] for k in reversed(list(a))}
    reordered["provider"]["local-libvirt"]["domain_xml_params"] = {
        "machine": a["provider"]["local-libvirt"]["domain_xml_params"]["machine"]
    }
    assert profile_digest(ProvisioningProfile.parse(a)) == profile_digest(
        ProvisioningProfile.parse(reordered)
    )


def test_profile_digest_differs_on_meaningful_change() -> None:
    a = ProvisioningProfile.parse(_valid())
    changed = _valid()
    changed["vcpu"] = 8
    assert profile_digest(a) != profile_digest(ProvisioningProfile.parse(changed))


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


@pytest.mark.parametrize("field", ["rootfs"])
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
def test_blank_crashkernel_rejected(value: str) -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["crashkernel"] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_rootfs_path_rejected(value: str) -> None:
    # A path-kind rootfs with a blank file path is as malformed as a blank string field was.
    data = _valid()
    data["provider"]["local-libvirt"]["rootfs"] = {"kind": "path", "path": value}
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


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_domain_xml_param_key_rejected(key: str) -> None:
    # An empty param name is as malformed as an empty value (ADR-0024 decision 2c).
    data = _valid()
    data["provider"]["local-libvirt"]["domain_xml_params"] = {key: "q35"}
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


def test_destructive_ops_defaults_empty() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.destructive_ops == []


def test_destructive_ops_accepts_force_crash() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.destructive_ops == ["force_crash"]


def test_destructive_ops_rejects_blank_entry() -> None:
    from kdive.domain.errors import CategorizedError

    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = [" "]
    with pytest.raises(CategorizedError):
        ProvisioningProfile.parse(data)


def test_debug_block_defaults_to_disabled() -> None:
    profile = ProvisioningProfile.parse(_valid())
    debug = profile.provider.local_libvirt.debug
    assert debug.preserve_on_crash is False
    assert debug.gdbstub is False


def test_debug_flags_parse_when_present() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"preserve_on_crash": True, "gdbstub": True}
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.debug.preserve_on_crash is True
    assert profile.provider.local_libvirt.debug.gdbstub is True


def test_debug_block_rejects_unknown_key() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"bogus": True}
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_crashkernel_is_optional() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["crashkernel"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.crashkernel is None
