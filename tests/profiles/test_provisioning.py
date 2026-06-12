"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import JobKind, ResourceKind
from kdive.domain.sizing import AllocationSizing
from kdive.profiles.provider_policy import policy_for_profile
from kdive.profiles.provisioning import (
    BootMethod,
    ProvisioningProfile,
    capture_method,
    destructive_opt_in,
    drgn_live_requires_credential,
    dump_profile,
    profile_digest,
    reconcile_profile_sizing,
    reject_rootfs_upload_without_window,
    require_concrete_sizing,
    rootfs_source,
    rootfs_upload_window_allowed,
    ssh_credential_ref,
    validate_profile,
)
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy

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
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
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
    assert rootfs.kind == "local"
    assert rootfs.path == "/var/lib/kdive/rootfs/fedora-40.qcow2"
    assert profile.provider.kind is ResourceKind.LOCAL_LIBVIRT


def test_policy_for_profile_resolves_local_libvirt_adapter() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert isinstance(policy_for_profile(profile), LocalLibvirtProfilePolicy)


def test_valid_fault_inject_profile_parses_and_dumps_alias() -> None:
    data = _valid()
    data["provider"] = {
        "fault-inject": {
            "capture_method": "host_dump",
            "destructive_ops": ["reprovision"],
        }
    }

    profile = ProvisioningProfile.parse(data)

    assert profile.provider.fault_inject.capture_method is CaptureMethod.HOST_DUMP
    assert profile.provider.kind is ResourceKind.FAULT_INJECT
    assert isinstance(policy_for_profile(profile), FaultInjectProfilePolicy)
    assert destructive_opt_in(profile, JobKind.REPROVISION) is True
    assert rootfs_upload_window_allowed(profile) is False
    assert dump_profile(profile)["provider"] == {
        "fault-inject": {
            "capture_method": "host_dump",
            "destructive_ops": ["reprovision"],
        }
    }


def test_provider_section_rejects_multiple_providers() -> None:
    data = _valid()
    data["provider"]["fault-inject"] = {}
    _expect_configuration_error(data)


def test_fault_inject_capture_method_defaults_to_console() -> None:
    data = _valid()
    data["provider"] = {"fault-inject": {}}
    assert capture_method(data) is CaptureMethod.CONSOLE


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
    assert ssh_credential_ref(profile) == "ssh/guest-key"


def test_ssh_credential_ref_returns_none_for_provider_without_ssh_credentials() -> None:
    data = _valid()
    data["provider"] = {"fault-inject": {}}
    profile = ProvisioningProfile.parse(data)

    assert ssh_credential_ref(profile) is None


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
        "boot_method",
        "kernel_source_ref",
        "provider",
    ],
)
def test_missing_core_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_sizing_fields_are_optional_at_parse(field: str) -> None:
    # ADR-0024 delta (ADR-0067): a shape-sized allocation omits profile sizing;
    # systems.provision constructs it from the resolved snapshot. Parsing is structural,
    # so an omitted sizing field is None, not an error.
    data = _valid()
    del data[field]
    parsed = ProvisioningProfile.parse(data)
    assert getattr(parsed, field) is None


@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_present_sizing_must_be_positive(field: str) -> None:
    data = _valid()
    data[field] = 0
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
    # A local-kind rootfs with a blank file path is as malformed as a blank string field was.
    data = _valid()
    data["provider"]["local-libvirt"]["rootfs"] = {"kind": "local", "path": value}
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


def test_destructive_ops_defaults_empty() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.destructive_ops == []


def test_destructive_ops_accepts_force_crash() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.destructive_ops == ["force_crash"]


def test_destructive_opt_in_reports_profile_gate() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)

    assert destructive_opt_in(profile, JobKind.FORCE_CRASH) is True
    assert destructive_opt_in(profile, JobKind.REPROVISION) is False


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


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"crashkernel": "256M"}, CaptureMethod.KDUMP),
        ({"debug": {"gdbstub": True}}, CaptureMethod.GDBSTUB),
        ({"debug": {"preserve_on_crash": True}}, CaptureMethod.HOST_DUMP),
        ({}, CaptureMethod.CONSOLE),
    ],
)
def test_capture_method_reports_profile_capture_tier(
    section: dict[str, Any], expected: CaptureMethod
) -> None:
    data = _valid()
    data["provider"]["local-libvirt"].pop("crashkernel")
    data["provider"]["local-libvirt"].update(section)

    assert capture_method(ProvisioningProfile.parse(data)) is expected


def test_capture_method_rejects_malformed_stored_mapping() -> None:
    with pytest.raises(CategorizedError) as exc:
        capture_method({"schema_version": 1})

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


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


def test_rootfs_upload_window_helpers_report_and_reject_upload_profiles() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["rootfs"] = {"kind": "upload"}
    profile = ProvisioningProfile.parse(data)

    assert rootfs_upload_window_allowed(profile) is True
    with pytest.raises(CategorizedError) as exc:
        reject_rootfs_upload_without_window(profile)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rootfs_upload_window_helpers_allow_non_upload_profiles() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert rootfs_upload_window_allowed(profile) is False
    reject_rootfs_upload_without_window(profile)


# --- ADR-0024 sizing reconciliation (#161) --------------------------------------------

_SNAPSHOT = AllocationSizing(vcpu=2, memory_mb=4096, disk_gb=20)


def test_reconcile_fills_omitted_sizing_from_snapshot() -> None:
    data = _valid()
    for field in ("vcpu", "memory_mb", "disk_gb"):
        del data[field]
    reconciled = reconcile_profile_sizing(data, _SNAPSHOT)
    parsed = ProvisioningProfile.parse(reconciled)
    assert (parsed.vcpu, parsed.memory_mb, parsed.disk_gb) == (2, 4096, 20)


def test_reconcile_accepts_matching_restatement() -> None:
    data = _valid()
    data["vcpu"], data["memory_mb"], data["disk_gb"] = 2, 4096, 20
    reconciled = reconcile_profile_sizing(data, _SNAPSHOT)
    assert (reconciled["vcpu"], reconciled["memory_mb"], reconciled["disk_gb"]) == (2, 4096, 20)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("vcpu", 99), ("memory_mb", 8192), ("disk_gb", 40)],
)
def test_reconcile_rejects_conflicting_restatement(field: str, bad: int) -> None:
    data = _valid()
    data["vcpu"], data["memory_mb"], data["disk_gb"] = 2, 4096, 20
    data[field] = bad
    with pytest.raises(CategorizedError) as caught:
        reconcile_profile_sizing(data, _SNAPSHOT)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_reconcile_does_not_mutate_input() -> None:
    data = _valid()
    del data["vcpu"]
    snapshot_before = copy.deepcopy(data)
    reconcile_profile_sizing(data, _SNAPSHOT)
    assert data == snapshot_before


def test_require_concrete_sizing_rejects_missing() -> None:
    data = _valid()
    del data["disk_gb"]
    parsed = ProvisioningProfile.parse(data)
    with pytest.raises(CategorizedError) as caught:
        require_concrete_sizing(parsed)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    missing = cast(list[str], caught.value.details["missing"])
    assert "disk_gb" in missing


def test_require_concrete_sizing_accepts_full() -> None:
    require_concrete_sizing(ProvisioningProfile.parse(_valid()))


_VALID_REMOTE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "remote-libvirt": {
            "base_image_volume": "kdive-base-fedora-42.qcow2",
            "crashkernel": "256M",
            "destructive_ops": ["force_crash"],
        }
    },
}


def _valid_remote() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid remote profile, safe to mutate."""
    return copy.deepcopy(_VALID_REMOTE)


def test_valid_remote_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert profile.provider.kind is ResourceKind.REMOTE_LIBVIRT
    assert isinstance(policy_for_profile(profile), RemoteLibvirtProfilePolicy)
    assert profile.boot_method is BootMethod.DISK_IMAGE
    section = profile.provider.remote_libvirt
    assert section.base_image_volume == "kdive-base-fedora-42.qcow2"
    assert section.crashkernel == "256M"


def test_remote_section_requires_disk_image_boot() -> None:
    data = _valid_remote()
    data["boot_method"] = "direct-kernel"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_disk_image_boot_requires_remote_section() -> None:
    data = _valid()
    data["boot_method"] = "disk-image"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_profile_capture_method_kdump_with_crashkernel() -> None:
    assert capture_method(ProvisioningProfile.parse(_valid_remote())) is CaptureMethod.KDUMP


def test_remote_profile_capture_method_gdbstub_without_crashkernel() -> None:
    data = _valid_remote()
    del data["provider"]["remote-libvirt"]["crashkernel"]

    assert capture_method(ProvisioningProfile.parse(data)) is CaptureMethod.GDBSTUB


def test_remote_profile_destructive_opt_in() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert destructive_opt_in(profile, JobKind.FORCE_CRASH) is True
    assert destructive_opt_in(profile, JobKind.REPROVISION) is False


def test_remote_profile_rootfs_and_ssh_are_none() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert rootfs_source(profile) is None
    assert ssh_credential_ref(profile) is None


def test_remote_profile_rejects_unknown_fields() -> None:
    data = _valid_remote()
    data["provider"]["remote-libvirt"]["bogus"] = "x"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_profile_validate_profile_accepts_remote_section() -> None:
    validate_profile(ProvisioningProfile.parse(_valid_remote()))


def test_drgn_live_requires_credential_true_for_local_section() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert drgn_live_requires_credential(profile) is True


def test_drgn_live_requires_credential_false_for_remote_section() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())
    assert drgn_live_requires_credential(profile) is False


def test_drgn_live_requires_credential_false_for_fault_inject_section() -> None:
    data = _valid()
    data["provider"] = {"fault-inject": {}}
    profile = ProvisioningProfile.parse(data)
    assert drgn_live_requires_credential(profile) is False
