"""Shared systems lifecycle validation helpers."""

from __future__ import annotations

from collections.abc import Callable

from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RootfsSource,
    _UploadRootfs,
    validate_profile,
)
from kdive.providers.component_validation import (
    ROOTFS_COMPONENT,
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)

type RootfsValidator = Callable[[RootfsSource], None]


def _validate_profile_for_provider(
    profile: ProvisioningProfile,
    capabilities: ComponentSourceCapabilities,
) -> None:
    validate_profile(profile)
    rootfs = profile.provider.local_libvirt.rootfs
    if isinstance(rootfs, _UploadRootfs):
        return
    reject_unsupported_component_source(
        capabilities,
        component_kind=ROOTFS_COMPONENT,
        ref=rootfs,
    )


def _validate_rootfs_for_provider(
    profile: ProvisioningProfile,
    rootfs_validator: RootfsValidator,
) -> None:
    rootfs = profile.provider.local_libvirt.rootfs
    if isinstance(rootfs, _UploadRootfs):
        return
    rootfs_validator(rootfs)
