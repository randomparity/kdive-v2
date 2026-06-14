"""Fault-inject provider composition tests."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
)
from kdive.providers.fault_inject import composition
from kdive.providers.fault_inject.build import FaultInjectBuild
from kdive.providers.fault_inject.debug.gdb import FaultInjectDebugEngine
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect
from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve


def test_discovery_registration_is_bind_only_and_targets_synthetic_host() -> None:
    registration = composition.discovery_registration()
    target = registration.target_factory()

    assert registration.kind is ResourceKind.FAULT_INJECT
    assert registration.pool_name == "fault-inject"
    assert registration.cost_class == "local"
    assert registration.creates is False
    assert target.resource_id == "fault-inject://local"


def test_build_reaper_wraps_the_supplied_inventory() -> None:
    inventory = FaultInjectInventory()
    domain = FaultInjectProvisioning(inventory).provision(
        uuid4(), profile=cast(ProvisioningProfile, object())
    )
    reaper = composition.build_reaper(inventory)

    assert isinstance(reaper, FaultInjectReaper)
    owned = asyncio.run(reaper.list_owned())
    assert [item.name for item in owned] == [domain]


def test_build_runtime_wires_fault_inject_ports_and_capabilities() -> None:
    runtime = composition.build_runtime(inventory=FaultInjectInventory())

    assert isinstance(runtime.profile_policy, FaultInjectProfilePolicy)
    assert isinstance(runtime.provisioner, FaultInjectProvisioning)
    assert isinstance(runtime.builder, FaultInjectBuild)
    assert isinstance(runtime.installer, FaultInjectInstall)
    assert isinstance(runtime.booter, FaultInjectInstall)
    assert isinstance(runtime.connector, FaultInjectConnect)
    assert isinstance(runtime.controller, FaultInjectControl)
    assert isinstance(runtime.retriever, FaultInjectRetrieve)
    assert isinstance(runtime.crash_postmortem, FaultInjectRetrieve)
    assert isinstance(runtime.vmcore_introspector, FaultInjectIntrospect)
    assert isinstance(runtime.live_introspector, FaultInjectIntrospect)
    assert runtime.supported_capture_methods == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
    assert runtime.debug is not None
    assert isinstance(runtime.debug.engine, FaultInjectDebugEngine)
    assert runtime.component_sources.provider == ResourceKind.FAULT_INJECT.value
    assert runtime.component_sources.accepted_component_sources == {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
