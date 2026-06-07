"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. Tool modules import ports and helper factories from here so provider-specific
imports stay out of the tool surface.
"""

from __future__ import annotations

from kdive.providers.local_libvirt.build import (
    LocalLibvirtBuild,
    validate_external_artifacts,
)
from kdive.providers.local_libvirt.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.control import LocalLibvirtControl
from kdive.providers.local_libvirt.debug_gdbmi import default_attach_seam
from kdive.providers.local_libvirt.install import LocalLibvirtInstall, read_console_log
from kdive.providers.local_libvirt.introspect_drgn import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    console_log_path,
    domain_name_for,
    reject_rootfs_without_upload_window,
    validate_profile,
)
from kdive.providers.local_libvirt.retrieve import (
    LocalLibvirtRetrieve,
    crash_command_rejection_reason,
)
from kdive.providers.ports import (
    AttachSeam,
    Booter,
    Builder,
    Connector,
    Controller,
    CrashPostmortem,
    Installer,
    LiveIntrospector,
    Provisioner,
    Retriever,
    VmcoreIntrospector,
)


def provisioner_from_env() -> Provisioner:
    return LocalLibvirtProvisioning.from_env()


def controller_from_env() -> Controller:
    return LocalLibvirtControl.from_env()


def builder_from_env() -> Builder:
    return LocalLibvirtBuild.from_env()


def install_boot_from_env() -> tuple[Installer, Booter]:
    install = LocalLibvirtInstall.from_env()
    return install, install


def connector_from_env() -> Connector:
    return LocalLibvirtConnect.from_env()


def attach_seam_from_env() -> AttachSeam:
    return default_attach_seam


def retriever_from_env() -> Retriever:
    return LocalLibvirtRetrieve.from_env()


def crash_postmortem_from_env() -> CrashPostmortem:
    return LocalLibvirtRetrieve.from_env()


def vmcore_introspector_from_env() -> VmcoreIntrospector:
    return LocalLibvirtVmcoreIntrospect.from_env()


def live_introspector_from_env() -> LiveIntrospector:
    return LocalLibvirtLiveIntrospect.from_env()


__all__ = [
    "builder_from_env",
    "attach_seam_from_env",
    "connector_from_env",
    "console_log_path",
    "controller_from_env",
    "crash_command_rejection_reason",
    "crash_postmortem_from_env",
    "domain_name_for",
    "install_boot_from_env",
    "live_introspector_from_env",
    "provisioner_from_env",
    "read_console_log",
    "reject_rootfs_without_upload_window",
    "retriever_from_env",
    "validate_external_artifacts",
    "validate_profile",
    "vmcore_introspector_from_env",
]
