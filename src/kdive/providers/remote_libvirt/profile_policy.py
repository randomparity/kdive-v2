"""Remote-libvirt provisioning-profile policy adapter."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import DestructiveJobKind
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource


class RemoteLibvirtProfilePolicy:
    """Behavior decisions owned by the remote-libvirt profile section."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        return None

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        return None

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        return False

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        return None

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        return op.value in profile.provider.remote_libvirt.destructive_ops

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        if profile.provider.remote_libvirt.crashkernel is not None:
            return CaptureMethod.KDUMP
        return CaptureMethod.GDBSTUB
