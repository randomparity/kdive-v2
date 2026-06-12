"""Provider-neutral helpers for parsed provisioning-profile policy decisions."""

from __future__ import annotations

from collections.abc import Mapping

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DestructiveJobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.runtime import ProfilePolicy


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def rootfs_source(policy: ProfilePolicy, profile: ProvisioningProfile) -> RootfsSource | None:
    """Return the profile's rootfs source, or ``None`` for providers that do not use one."""
    return policy.rootfs_source(profile)


def rootfs_upload_window_allowed(policy: ProfilePolicy, profile: ProvisioningProfile) -> bool:
    """Return whether the profile's rootfs expects a System upload window."""
    rootfs = rootfs_source(policy, profile)
    return rootfs is not None and rootfs.kind == "upload"


def reject_rootfs_upload_without_window(
    policy: ProfilePolicy, profile: ProvisioningProfile
) -> None:
    """Reject a profile whose rootfs needs a System upload window in a no-window lane."""
    if rootfs_upload_window_allowed(policy, profile):
        raise CategorizedError(
            "upload-kind rootfs requires systems.define upload window",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def ssh_credential_ref(policy: ProfilePolicy, profile: ProvisioningProfile) -> str | None:
    """Return the SSH credential reference for providers with credential-backed SSH."""
    return policy.ssh_credential_ref(profile)


def drgn_live_requires_credential(policy: ProfilePolicy, profile: ProvisioningProfile) -> bool:
    """Return whether this profile's drgn-live transport needs a core-resolved credential."""
    return policy.drgn_live_requires_credential(profile)


def validate_profile(policy: ProfilePolicy, profile: ProvisioningProfile) -> None:
    """Reject unsupported provider params and unresolvable rootfs references."""
    policy.validate_profile(profile)


def destructive_opt_in(
    policy: ProfilePolicy, profile: ProvisioningProfile, op: DestructiveJobKind
) -> bool:
    """Return whether the profile opts into a destructive operation."""
    return policy.destructive_opt_in(profile, op)


def capture_method(
    policy: ProfilePolicy, profile: ProvisioningProfile | Mapping[str, object]
) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables."""
    parsed = _parsed_profile(profile)
    return policy.capture_method(parsed)
