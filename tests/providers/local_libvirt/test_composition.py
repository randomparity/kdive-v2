"""Local-libvirt provider composition tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
)
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.local_libvirt import composition
from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.debug.introspect import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.security.secrets.secret_registry import SecretRegistry


def test_discovery_registration_targets_local_libvirt() -> None:
    registration = composition.discovery_registration()
    target = registration.target_factory()

    assert registration.kind is ResourceKind.LOCAL_LIBVIRT
    assert registration.pool_name == "local-libvirt"
    assert registration.cost_class == "local"
    assert registration.creates is True
    assert isinstance(target.discovery, LocalLibvirtDiscovery)
    assert target.resource_id == target.discovery.host_uri


def test_build_reaper_is_local_libvirt_reaper() -> None:
    assert isinstance(composition.build_reaper(), LibvirtInfraReaper)


def test_build_runtime_wires_local_ports_and_capabilities() -> None:
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    assert isinstance(runtime.profile_policy, LocalLibvirtProfilePolicy)
    assert isinstance(runtime.provisioner, LocalLibvirtProvisioning)
    assert isinstance(runtime.builder, LocalLibvirtBuild)
    assert isinstance(runtime.installer, LocalLibvirtInstall)
    assert isinstance(runtime.booter, LocalLibvirtInstall)
    assert isinstance(runtime.connector, LocalLibvirtConnect)
    assert isinstance(runtime.controller, LocalLibvirtControl)
    assert isinstance(runtime.retriever, LocalLibvirtRetrieve)
    assert isinstance(runtime.crash_postmortem, LocalLibvirtRetrieve)
    assert isinstance(runtime.vmcore_introspector, LocalLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, LocalLibvirtLiveIntrospect)
    assert isinstance(runtime.rootfs_build_plane, LocalLibvirtRootfsBuildPlane)
    assert runtime.supported_capture_methods == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, GdbMiEngine)
    assert runtime.component_sources.provider == ResourceKind.LOCAL_LIBVIRT.value
    assert runtime.component_sources.accepted_component_sources == {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
    assert runtime.build_config_validator is not None
    assert runtime.rootfs_validator is not None
