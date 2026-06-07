"""Tests for provider runtime composition and dispatch."""

from __future__ import annotations

from uuid import UUID

from kdive.domain.models import ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.capability import (
    Capability,
    CapabilityRegistry,
    CleanupGuarantee,
    OpContract,
    Plane,
)
from kdive.providers.composition import ProviderRuntime
from kdive.providers.local_libvirt.build import BuildOutput

_RUN = UUID("22222222-2222-2222-2222-222222222222")


def _contract() -> OpContract:
    return OpContract(
        idempotent=True,
        destructive=False,
        cancelable=False,
        long_running=True,
        cleanup=CleanupGuarantee.BEST_EFFORT,
    )


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


def test_provider_runtime_builder_dispatches_through_capability_registry() -> None:
    provider = _BuildProvider()
    registry = CapabilityRegistry()
    registry.register(
        provider,
        [
            Capability(
                plane=Plane.BUILD,
                operation="build",
                resource_kind=ResourceKind.LOCAL_LIBVIRT,
                contract=_contract(),
            )
        ],
        provider_id="test-build",
        health=ResourceStatus.AVAILABLE,
        cost_class="test",
    )

    output = ProviderRuntime(registry).builder().build(_RUN, _build_profile())

    assert output.build_id == "deadbeef"
    assert provider.calls == [(_RUN, "file:///configs/kdump.config")]
