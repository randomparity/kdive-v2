"""Tests for provider runtime composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind, Sensitivity
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.provider_components.artifacts import StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    PATCH_COMPONENT,
    LocalComponentRef,
)
from kdive.providers import composition
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.ports import (
    CaptureOutput,
    CrashOutput,
    InstallRequest,
    IntrospectOutput,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.reaping import OwnedDomain
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
from kdive.providers.remote_libvirt.rootfs_build import RemoteLibvirtRootfsBuildPlane
from kdive.providers.runtime import ProviderRuntime
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("22222222-2222-2222-2222-222222222222")

_REMOTE_INVENTORY = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
[[remote_libvirt]]
name = "host"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "clientcert.pem"
client_key_ref = "clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "cacert.pem"
base_image = "base"
cost_class = "remote"
"""


def _declare_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(_REMOTE_INVENTORY)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()


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
        profile_policy=LocalLibvirtProfilePolicy(),
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

    assert isinstance(runtime.profile_policy, LocalLibvirtProfilePolicy)
    assert runtime.component_sources.provider == "local-libvirt"
    assert runtime.component_sources.accepted_component_sources == {
        "rootfs": frozenset({"catalog", "local"}),
        "kernel": frozenset({"local"}),
        "initrd": frozenset({"local"}),
        "config": frozenset({"catalog", "local"}),
        "patch": frozenset({"local"}),
        "vmlinux": frozenset({"local"}),
    }


def test_default_runtime_exposes_build_config_validator() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert runtime.build_config_validator is not None


def test_default_runtime_exposes_rootfs_build_plane() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.rootfs_build_plane, LocalLibvirtRootfsBuildPlane)


def test_provider_runtime_discovery_hook_is_optional() -> None:
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    calls: list[AsyncConnectionPool] = []

    async def _register(pool: AsyncConnectionPool) -> None:
        calls.append(pool)

    runtime = ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
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
        profile_policy=LocalLibvirtProfilePolicy(),
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
    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)  # default = opt-in OFF
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)  # same for remote
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
    runtime = composition.build_fault_inject_runtime()

    assert isinstance(runtime.profile_policy, FaultInjectProfilePolicy)
    assert runtime.component_sources.provider == "fault-inject"
    assert runtime.discovery_registrar is not None


def test_fault_inject_runtime_provision_is_visible_to_a_reaper_on_the_same_inventory() -> None:
    import asyncio
    from uuid import UUID

    from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

    inventory = FaultInjectInventory()
    runtime = composition.build_fault_inject_runtime(inventory=inventory)
    system_id = UUID("33333333-3333-3333-3333-333333333333")

    domain = runtime.provisioner.provision(system_id, _provisioning_profile())

    # The shared-inventory seam: a domain the runtime provisions is reapable through a
    # FaultInjectReaper built over the same inventory (the reconciler leaked-domain seam).
    owned = asyncio.run(FaultInjectReaper(inventory).list_owned())
    assert [d.name for d in owned] == [domain]


@dataclass(frozen=True)
class _FakeOwnedDomain:
    """An OwnedDomain stand-in (structural: ``name`` + ``system_id``)."""

    name: str
    system_id: UUID | None = None


class _FakeLibvirtReaper:
    """A hermetic stand-in for the libvirt-backed reaper (no live connection in tests)."""

    def __init__(self, *owned: OwnedDomain) -> None:
        self._owned: list[OwnedDomain] = list(owned)
        self.destroyed: list[str] = []

    async def list_owned(self) -> list[OwnedDomain]:
        return list(self._owned)

    async def destroy(self, name: str) -> None:
        self.destroyed.append(name)


def test_reconciler_reaper_is_libvirt_backed_without_fault_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #372: a stock deployment's reaper is the libvirt-backed reaper (not NullReaper), so a
    # name-orphaned domain reaches repair_leaked_domains. (Previously asserted NullReaper —
    # that encoded the inert-predicate bug.)
    import asyncio

    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)
    owner = composition.ProviderComposition()

    sentinel = _FakeOwnedDomain(name="kdive-sentinel")
    reaper = owner.build_reconciler_reaper(libvirt_reaper=_FakeLibvirtReaper(sentinel))

    # No fault-inject → the single libvirt reaper is returned directly (not composed/Null).
    assert asyncio.run(reaper.list_owned()) == [sentinel]


def test_configured_fault_inject_runtime_is_visible_to_reconciler_reaper() -> None:
    import asyncio
    from uuid import UUID

    from kdive.domain.models import ResourceKind

    owner = composition.ProviderComposition()
    resolver = owner.build_provider_resolver(enable_fault_inject=True)
    # Inject a hermetic libvirt reaper so the composite never opens a live qemu:/// connection.
    reaper = owner.build_reconciler_reaper(
        enable_fault_inject=True, libvirt_reaper=_FakeLibvirtReaper()
    )
    system_id = UUID("44444444-4444-4444-4444-444444444444")

    domain = resolver.resolve(ResourceKind.FAULT_INJECT).provisioner.provision(
        system_id, _provisioning_profile()
    )

    # The composite unions the (empty) libvirt reaper rows with the fault-inject rows, so the
    # fault-inject domain is still visible and reapable.
    owned = asyncio.run(reaper.list_owned())
    assert domain in [item.name for item in owned]
    asyncio.run(reaper.destroy(domain))


def test_transport_resetter_is_null_without_remote() -> None:
    from kdive.providers.transport_reset import NullResetter

    comp = composition.ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=False)
    assert isinstance(resetter, NullResetter)


def test_transport_resetter_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter

    comp = composition.ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=True)
    assert isinstance(resetter, RemoteLibvirtTransportResetter)


def test_dump_volume_reaper_is_null_without_remote() -> None:
    from kdive.providers.reaping import NullDumpVolumeReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_dump_volume_reaper(enable_remote_libvirt=False)
    assert isinstance(reaper, NullDumpVolumeReaper)


def test_dump_volume_reaper_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.dump_volume_reaper import RemoteLibvirtDumpVolumeReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_dump_volume_reaper(enable_remote_libvirt=True)
    assert isinstance(reaper, RemoteLibvirtDumpVolumeReaper)


def test_console_hosting_is_none_without_remote() -> None:
    import asyncio

    comp = composition.ProviderComposition()

    assert asyncio.run(comp.build_reconciler_console_hosting(enable_remote_libvirt=False)) is None


def test_build_host_prober_is_wired_independent_of_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSH build-host prober is built unconditionally — not gated on remote-libvirt."""
    from kdive.providers.build_host.reachability import BuildHostProber, SshBuildHostProber
    from kdive.providers.remote_libvirt import config as remote_config

    # Force remote-libvirt to read as unconfigured; the prober must still be returned.
    monkeypatch.setattr(remote_config, "is_remote_libvirt_configured", lambda: False)

    comp = composition.ProviderComposition()
    prober = comp.build_reconciler_build_host_prober()
    assert isinstance(prober, SshBuildHostProber)
    assert isinstance(prober, BuildHostProber)


def test_console_hosting_delegates_to_remote_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    expected_hosting = object()
    expected_registry = SecretRegistry()
    seen: dict[str, object] = {}

    async def _build_console_hosting(
        *, secret_registry: SecretRegistry, running_systems_factory: object
    ) -> object:
        seen["secret_registry"] = secret_registry
        seen["running_systems_factory"] = running_systems_factory
        return expected_hosting

    monkeypatch.setattr(
        composition.remote_composition, "build_console_hosting", _build_console_hosting
    )

    comp = composition.ProviderComposition(secret_registry=expected_registry)

    assert (
        asyncio.run(comp.build_reconciler_console_hosting(enable_remote_libvirt=True))
        is expected_hosting
    )
    assert seen["secret_registry"] is expected_registry
    assert seen["running_systems_factory"] is composition.DbRunningRemoteSystems


def test_fault_inject_opt_in_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.domain.models import ResourceKind

    monkeypatch.setenv("KDIVE_FAULT_INJECT", "1")

    resolver = composition.build_provider_resolver()

    assert ResourceKind.FAULT_INJECT in resolver.registered_kinds()


def test_fault_inject_runtime_without_engine_uses_bare_happy_path_ports() -> None:
    from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
    from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning

    runtime = composition.build_fault_inject_runtime()

    # No engine -> the happy-path ports are used unchanged (no faulting wrapper).
    assert isinstance(runtime.provisioner, FaultInjectProvisioning)
    assert isinstance(runtime.installer, FaultInjectInstall)
    assert isinstance(runtime.booter, FaultInjectInstall)


def test_fault_inject_runtime_with_engine_wraps_ports_in_faulting_decorators() -> None:
    from kdive.providers.fault_inject.faulting.engine import FaultEngine
    from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvisioning

    engine = FaultEngine(seed=7, fault_rate={"provision": 1.0}, max_latency_s={})
    runtime = composition.build_fault_inject_runtime(engine=engine)

    assert isinstance(runtime.provisioner, FaultedProvisioning)
    assert isinstance(runtime.installer, FaultedInstall)
    assert isinstance(runtime.booter, FaultedInstall)


def test_remote_libvirt_registers_via_inventory_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_remote(tmp_path, monkeypatch)

    resolver = composition.build_provider_resolver()

    assert ResourceKind.REMOTE_LIBVIRT in resolver.registered_kinds()


def test_remote_libvirt_explicit_flag_wins_over_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_remote(tmp_path, monkeypatch)

    resolver = composition.build_provider_resolver(enable_remote_libvirt=False)

    assert ResourceKind.REMOTE_LIBVIRT not in resolver.registered_kinds()


def test_remote_libvirt_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    resolver = composition.build_provider_resolver()

    assert ResourceKind.REMOTE_LIBVIRT not in resolver.registered_kinds()


def test_remote_runtime_buildable_without_operator_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    # Buildability gates only construction (ADR-0076); config gates discovery/connection.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.discovery_registrar is not None


def test_remote_runtime_advertises_all_four_capture_methods() -> None:
    # M2.5 brings remote to 4/4 advertised methods: the two-phase kdump path (ADR-0084), the
    # host-side core-dump host_dump path (ADR-0094, #301), the already-wired gdbstub transport
    # (ADR-0083/0085, #302), and the reconciler-owned console collector (#303, ADR-0095).
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.supported_capture_methods == frozenset(
        {
            CaptureMethod.KDUMP,
            CaptureMethod.HOST_DUMP,
            CaptureMethod.GDBSTUB,
            CaptureMethod.CONSOLE,
        }
    )


def test_remote_runtime_advertises_host_dump_as_a_capture_method() -> None:
    # #301: HOST_DUMP is in vmcore.fetch's _VMCORE_METHODS, so advertising it admits
    # vmcore.fetch(method=host_dump) on remote through the existing tool.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.HOST_DUMP in runtime.supported_capture_methods


def test_remote_runtime_advertises_gdbstub_as_a_capture_method() -> None:
    # AC2: GDBSTUB is counted by the advertised capability surface. gdbstub is not
    # consumed through vmcore.fetch (only HOST_DUMP/KDUMP are), so there is no selection
    # path to gate; the assertion is membership in the advertised set.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.GDBSTUB in runtime.supported_capture_methods


def test_remote_runtime_advertises_console_as_a_capture_method() -> None:
    # #303 (ADR-0095): CONSOLE is in the advertised set so the reconciler-owned collector's
    # artifact is selectable. Like gdbstub, console is consumed off the boot/diagnostic plane,
    # not through vmcore.fetch, so the assertion is membership in the advertised set.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.CONSOLE in runtime.supported_capture_methods


def test_remote_runtime_gdbstub_debug_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC2 no-regression: advertising GDBSTUB does not alter the existing connect/attach
    # debug path (ADR-0083/0085) — the remote attach seam and connector are unchanged.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
    from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.debug is not None
    assert runtime.debug.attach_seam is remote_attach_seam
    assert isinstance(runtime.connector, RemoteLibvirtConnect)


def test_remote_runtime_has_real_control_and_retrieve() -> None:
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.profile_policy, RemoteLibvirtProfilePolicy)
    assert isinstance(runtime.controller, RemoteLibvirtControl)
    assert isinstance(runtime.retriever, RemoteLibvirtRetrieve)
    assert runtime.crash_postmortem is runtime.retriever


def test_remote_runtime_has_real_provisioner(monkeypatch: pytest.MonkeyPatch) -> None:
    # The provisioning plane is real from this issue on; it must construct without
    # any operator config (config is read per op, ADR-0076/0080).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.provisioner, RemoteLibvirtProvisioning)


def test_remote_runtime_has_noop_rootfs_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    # The systems registrar hard-fails on rootfs_validator=None, so the remote runtime
    # must supply the no-op contract (a remote profile has no rootfs; it is never
    # invoked) - the fault-inject precedent.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.rootfs_validator is not None


def test_remote_runtime_has_real_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    # The remote Build plane is real from this issue on (ADR-0081); it must construct
    # without operator config (the build env is read per op).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.builder, RemoteLibvirtBuild)


def test_remote_runtime_exposes_rootfs_build_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.rootfs_build_plane, RemoteLibvirtRootfsBuildPlane)


def test_remote_runtime_has_real_installer_and_booter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The remote Install/Boot plane is real from this issue on (ADR-0082); it must construct
    # without operator config, and one object realizes both ports (as local does).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.installer, RemoteLibvirtInstall)
    assert runtime.booter is runtime.installer


def test_remote_runtime_wires_connect_and_introspect_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The connect/debug + introspection planes are real (ADR-0083); control/retrieve are
    # real from issue #206 on (ADR-0084), asserted in test_remote_runtime_has_real_control_*.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
    from kdive.providers.remote_libvirt.debug.introspect import (
        RemoteLibvirtLiveIntrospect,
        RemoteLibvirtVmcoreIntrospect,
    )
    from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.connector, RemoteLibvirtConnect)
    assert runtime.debug is not None
    assert runtime.debug.attach_seam is remote_attach_seam
    assert isinstance(runtime.vmcore_introspector, RemoteLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, RemoteLibvirtLiveIntrospect)


def test_remote_runtime_wires_build_config_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    # runs.build runs the config validator after the component-source gate; without it a
    # remote build's config ref goes unvalidated. It must be the builder's validator.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.build_config_validator is not None


def test_remote_runtime_accepts_local_and_catalog_config_and_local_patch_sources() -> None:
    # runs.build rejects a config whose source-kind is not advertised; an empty set rejects
    # every remote build. The remote server build merges a kdump fragment from a local .config
    # or the seeded catalog entry + applies an optional local patch, so it advertises CONFIG as
    # {"catalog", "local"} and PATCH as {"local"} (ADR-0081/0096).
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    accepted = runtime.component_sources.accepted_component_sources
    assert accepted.get(CONFIG_COMPONENT) == frozenset({"catalog", "local"})
    assert accepted.get(PATCH_COMPONENT) == frozenset({"local"})
    assert runtime.component_sources.provider == ResourceKind.REMOTE_LIBVIRT.value
