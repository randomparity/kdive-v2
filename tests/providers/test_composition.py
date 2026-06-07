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
from kdive.providers import composition
from kdive.providers.composition import ProviderRuntime
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.providers.ports import BuildOutput, CaptureOutput, CrashOutput, IntrospectOutput
from kdive.store.objectstore import StoredArtifact

_RUN = UUID("22222222-2222-2222-2222-222222222222")


def _build_profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "file:///src/linux",
            "config_ref": "file:///configs/kdump.config",
            "patch_ref": None,
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


class _BuildProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        self.calls.append((run_id, profile.config_ref))
        return BuildOutput(kernel_ref="k", debuginfo_ref="v", build_id="deadbeef")


class _ProvisionProvider:
    def provision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down = domain_name

    def reprovision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"


class _InstallProvider:
    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        self.installed = (system_id, run_id, kernel_ref, cmdline, method, initrd_ref)

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
    assert builder.calls == [(_RUN, "file:///configs/kdump.config")]
    assert runtime.install_boot() == (install, install)


def test_default_runtime_does_not_build_unused_capability_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailRegistry:
        def __init__(self) -> None:
            raise AssertionError("default typed runtime should not build a capability registry")

    monkeypatch.setattr(composition, "CapabilityRegistry", _FailRegistry, raising=False)

    runtime = composition.build_default_provider_runtime()

    assert runtime.builder is not None


def test_default_runtime_advertises_implemented_component_sources_only() -> None:
    runtime = composition.build_default_provider_runtime()

    assert runtime.component_sources.provider == "local-libvirt"
    assert runtime.component_sources.accepted_component_sources == {
        "rootfs": frozenset({"local"}),
        "kernel": frozenset({"local"}),
        "initrd": frozenset({"local"}),
        "config": frozenset({"local"}),
        "patch": frozenset({"local"}),
        "vmlinux": frozenset({"local"}),
    }


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
