"""Provider policy interface for parsed provisioning profiles."""

from __future__ import annotations

from typing import Protocol

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import DestructiveJobKind, ResourceKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy


class ProfilePolicy(Protocol):
    """Provider-owned behavior derived from a parsed provisioning profile."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        """Return the rootfs source used by this provider, if any."""

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        """Return the live-SSH credential reference used by this provider, if any."""

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        """Return whether drgn-live needs a profile credential."""

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        """Run provider-specific static profile validation."""

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        """Return whether the profile opts into a destructive operation."""

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        """Resolve the crash-capture method enabled by the profile."""


_POLICIES: dict[ResourceKind, ProfilePolicy] = {
    ResourceKind.LOCAL_LIBVIRT: LocalLibvirtProfilePolicy(),
    ResourceKind.FAULT_INJECT: FaultInjectProfilePolicy(),
    ResourceKind.REMOTE_LIBVIRT: RemoteLibvirtProfilePolicy(),
}


def policy_for_profile(profile: ProvisioningProfile) -> ProfilePolicy:
    """Return the provider-owned policy adapter for a parsed profile."""
    return _POLICIES[profile.provider.kind]
