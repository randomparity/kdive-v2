"""Local-libvirt provisioning-profile policy adapter."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DestructiveJobKind
from kdive.profiles.provisioning import (
    SUPPORTED_DOMAIN_XML_PARAMS,
    ProvisioningProfile,
    RootfsSource,
    validate_rootfs_reference,
)


class LocalLibvirtProfilePolicy:
    """Behavior decisions owned by the local-libvirt profile section."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource:
        return profile.provider.local_libvirt.rootfs

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        return profile.provider.local_libvirt.ssh_credential_ref

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        return True

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        section = profile.provider.local_libvirt
        unknown = sorted(set(section.domain_xml_params) - SUPPORTED_DOMAIN_XML_PARAMS)
        if unknown:
            raise CategorizedError(
                f"unsupported domain_xml_params: {', '.join(unknown)}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
            )
        validate_rootfs_reference(section.rootfs)

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        return op.value in profile.provider.local_libvirt.destructive_ops

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        section = profile.provider.local_libvirt
        if section.crashkernel is not None:
            return CaptureMethod.KDUMP
        if section.debug.gdbstub:
            return CaptureMethod.GDBSTUB
        if section.debug.preserve_on_crash:
            return CaptureMethod.HOST_DUMP
        return CaptureMethod.CONSOLE
