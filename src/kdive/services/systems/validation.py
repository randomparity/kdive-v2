"""Provider-aware systems profile validation."""

from __future__ import annotations

from collections.abc import Callable

from kdive.profiles.provider_policy import rootfs_source, validate_profile
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource, _UploadRootfs
from kdive.provider_components.references import ROOTFS_COMPONENT
from kdive.provider_components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.providers.runtime import ProfilePolicy

type RootfsValidator = Callable[[RootfsSource], None]


def validate_profile_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    capabilities: ComponentSourceCapabilities,
) -> None:
    validate_profile(profile_policy, profile)
    rootfs = rootfs_source(profile_policy, profile)
    if rootfs is None:
        return
    if isinstance(rootfs, _UploadRootfs):
        return
    reject_unsupported_component_source(
        capabilities,
        component_kind=ROOTFS_COMPONENT,
        ref=rootfs,
    )


def validate_rootfs_for_provider(
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> None:
    rootfs = rootfs_source(profile_policy, profile)
    if rootfs is None:
        return
    if isinstance(rootfs, _UploadRootfs):
        return
    rootfs_validator(rootfs)
