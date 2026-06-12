"""Provider-neutral helpers for parsed provisioning-profile policy decisions."""

from __future__ import annotations

from collections.abc import Mapping

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.runtime import ProfilePolicy


def _parsed_profile(profile: ProvisioningProfile | Mapping[str, object]) -> ProvisioningProfile:
    if isinstance(profile, ProvisioningProfile):
        return profile
    return ProvisioningProfile.parse(profile)


def rootfs_upload_window_allowed(policy: ProfilePolicy, profile: ProvisioningProfile) -> bool:
    """Return whether the profile's rootfs expects a System upload window."""
    rootfs = policy.rootfs_source(profile)
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


def capture_method(
    policy: ProfilePolicy, profile: ProvisioningProfile | Mapping[str, object]
) -> CaptureMethod:
    """Resolve the crash-capture method a provisioning profile enables."""
    parsed = _parsed_profile(profile)
    return policy.capture_method(parsed)
