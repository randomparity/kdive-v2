"""Tests for provider runtime composition."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import Sensitivity
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.provider_components.artifacts import StoredArtifact
from kdive.provider_components.references import LocalComponentRef
from kdive.providers import composition
from kdive.providers.ports import (
    BuildOutput,
    CaptureOutput,
    CrashOutput,
    InstallRequest,
    IntrospectOutput,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.runtime import ProviderRuntime
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("22222222-2222-2222-2222-222222222222")


def _build_profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "file:///src/linux",
            "config": {"kind": "local", "path": "/configs/kdump.config"},
            "patch_ref": None,
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _provisioning_profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 1,
            "memory_mb": 1024,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"},
                }
            },
        }
    )


class _BuildProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        assert isinstance(profile.config, LocalComponentRef)
        self.calls.append((run_id, profile.config.path))
        return BuildOutput(kernel_ref="k", debuginfo_ref="v", build_id="deadbeef")


class _ProvisionProvider:
    def provision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down = domain_name

    def reprovision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"


class _InstallProvider:
    def install(self, request: InstallRequest) -> None:
        self.installed = request

    def boot(self, system_id: UUID) -> None:
        self.booted = system_id


class _ConnectorProvider:
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        return TransportHandle(f"{kind}://{system}")

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed = handle


class _ControllerProvider:
    def power(self, domain_name: str, action: object) -> None:
        self.powered = (domain_name, action)

    def force_crash(self, domain_name: str) -> None:
        self.crashed = domain_name


class _RetrieveProvider:
    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        artifact = StoredArtifact("key", "etag", Sensitivity.SENSITIVE, "vmcore")
        return CaptureOutput(raw=artifact, redacted=artifact, vmcore_build_id="deadbeef")

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        return CrashOutput(results={}, transcript="", truncated=False)


class _IntrospectorProvider:
    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)


def test_provider_runtime_returns_typed_provider_ports_directly() -> None:
    builder = _BuildProvider()
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    runtime = ProviderRuntime(
        provisioner=_ProvisionProvider(),
        builder=builder,
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
    )

    output = runtime.builder.build(_RUN, _build_profile())

    assert output.build_id == "deadbeef"
    assert builder.calls == [(_RUN, "/configs/kdump.config")]
    assert runtime.installer is install
    assert runtime.booter is install


def test_default_runtime_advertises_implemented_component_sources_only() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert runtime.component_sources.provider == "local-libvirt"
    assert runtime.component_sources.accepted_component_sources == {
        "rootfs": frozenset({"catalog", "local"}),
        "kernel": frozenset({"local"}),
        "initrd": frozenset({"local"}),
        "config": frozenset({"local"}),
        "patch": frozenset({"local"}),
        "vmlinux": frozenset({"local"}),
    }


def test_default_runtime_exposes_build_config_validator() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert runtime.build_config_validator is not None


def test_provider_runtime_discovery_hook_is_optional() -> None:
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    calls: list[AsyncConnectionPool] = []

    async def _register(pool: AsyncConnectionPool) -> None:
        calls.append(pool)

    runtime = ProviderRuntime(
        provisioner=_ProvisionProvider(),
        builder=_BuildProvider(),
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
        discovery_registrar=_register,
    )
    pool = cast(AsyncConnectionPool, object())

    asyncio.run(runtime.register_discovery(pool))

    assert calls == [pool]


def test_provider_runtime_discovery_hook_noops_when_absent() -> None:
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    runtime = ProviderRuntime(
        provisioner=_ProvisionProvider(),
        builder=_BuildProvider(),
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
    )

    asyncio.run(runtime.register_discovery(cast(AsyncConnectionPool, object())))


def test_default_resolver_registers_only_local_libvirt(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.domain.models import ResourceKind

    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)  # default = opt-in OFF
    resolver = composition.build_provider_resolver()
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})
    local = resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    assert local.component_sources.provider == "local-libvirt"


def test_enabling_fault_inject_registers_both_kinds() -> None:
    from kdive.domain.models import ResourceKind

    resolver = composition.build_provider_resolver(enable_fault_inject=True)

    assert resolver.registered_kinds() == frozenset(
        {ResourceKind.LOCAL_LIBVIRT, ResourceKind.FAULT_INJECT}
    )


def test_fault_inject_runtime_advertises_its_provider_identity() -> None:
    runtime = composition.build_faultinject_runtime()

    assert runtime.component_sources.provider == "fault-inject"
    assert runtime.discovery_registrar is not None


def test_fault_inject_runtime_provision_is_visible_to_a_reaper_on_the_same_inventory() -> None:
    import asyncio
    from uuid import UUID

    from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

    inventory = FaultInjectInventory()
    runtime = composition.build_faultinject_runtime(inventory=inventory)
    system_id = UUID("33333333-3333-3333-3333-333333333333")

    domain = runtime.provisioner.provision(system_id, _provisioning_profile())

    # The shared-inventory seam: a domain the runtime provisions is reapable through a
    # FaultInjectReaper built over the same inventory (the reconciler leaked-domain seam).
    owned = asyncio.run(FaultInjectReaper(inventory).list_owned())
    assert [d.name for d in owned] == [domain]


def test_configured_fault_inject_runtime_is_visible_to_reconciler_reaper() -> None:
    import asyncio
    from uuid import UUID

    from kdive.domain.models import ResourceKind

    owner = composition.ProviderComposition()
    resolver = owner.build_provider_resolver(enable_fault_inject=True)
    reaper = owner.build_reconciler_reaper(enable_fault_inject=True)
    system_id = UUID("44444444-4444-4444-4444-444444444444")

    domain = resolver.resolve(ResourceKind.FAULT_INJECT).provisioner.provision(
        system_id, _provisioning_profile()
    )

    owned = asyncio.run(reaper.list_owned())
    assert domain in [item.name for item in owned]
    asyncio.run(reaper.destroy(domain))


def test_reconciler_reaper_defaults_to_null_when_fault_inject_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.providers.reaping import NullReaper

    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)

    owner = composition.ProviderComposition()

    assert isinstance(owner.build_reconciler_reaper(), NullReaper)


def test_fault_inject_opt_in_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.domain.models import ResourceKind

    monkeypatch.setenv("KDIVE_FAULT_INJECT", "1")

    resolver = composition.build_provider_resolver()

    assert ResourceKind.FAULT_INJECT in resolver.registered_kinds()


def test_fault_inject_runtime_without_engine_uses_bare_happy_path_ports() -> None:
    from kdive.providers.fault_inject.lifecycle.provider import (
        FaultInjectInstall,
        FaultInjectProvision,
    )

    runtime = composition.build_faultinject_runtime()

    # No engine -> the happy-path ports are used unchanged (no faulting wrapper).
    assert isinstance(runtime.provisioner, FaultInjectProvision)
    assert isinstance(runtime.installer, FaultInjectInstall)
    assert isinstance(runtime.booter, FaultInjectInstall)


def test_fault_inject_runtime_with_engine_wraps_ports_in_faulting_decorators() -> None:
    from kdive.providers.fault_inject.faulting.engine import FaultEngine
    from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvision

    engine = FaultEngine(seed=7, fault_rate={"provision": 1.0}, max_latency_s={})
    runtime = composition.build_faultinject_runtime(engine=engine)

    assert isinstance(runtime.provisioner, FaultedProvision)
    assert isinstance(runtime.installer, FaultedInstall)
    assert isinstance(runtime.booter, FaultedInstall)
