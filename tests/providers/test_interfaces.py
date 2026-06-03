"""Tests for the plane Protocols (ADR-0009 / ADR-0022, issue #13)."""

from __future__ import annotations

import kdive.providers.interfaces as interfaces
from kdive.providers.capability import Plane
from kdive.providers.interfaces import (
    BuildPlane,
    ConnectPlane,
    ControlPlane,
    DebugPlane,
    DiscoveryPlane,
    InstallPlane,
    ProvisioningPlane,
    RetrievePlane,
)
from tests.providers.conftest import FakeProvider, PartialFakeProvider


def _assert_static_conformance(
    discovery: DiscoveryPlane,
    provisioning: ProvisioningPlane,
    build: BuildPlane,
    install: InstallPlane,
    connect: ConnectPlane,
    debug: DebugPlane,
    control: ControlPlane,
    retrieve: RetrievePlane,
) -> None:
    """`ty` checks each argument satisfies its Protocol at the call site below."""


def test_full_fake_satisfies_every_plane_protocol() -> None:
    provider = FakeProvider()
    # Static signature gate (checked by ty): a mismatch fails the typecheck.
    _assert_static_conformance(
        provider, provider, provider, provider, provider, provider, provider, provider
    )
    # Runtime presence smoke-test (runtime_checkable checks method names only).
    for plane in (
        DiscoveryPlane,
        ProvisioningPlane,
        BuildPlane,
        InstallPlane,
        ConnectPlane,
        DebugPlane,
        ControlPlane,
        RetrievePlane,
    ):
        assert isinstance(provider, plane)


def test_partial_fake_satisfies_only_implemented_planes() -> None:
    provider = PartialFakeProvider()
    assert isinstance(provider, BuildPlane)
    assert isinstance(provider, DiscoveryPlane)
    assert not isinstance(provider, ControlPlane)
    assert not isinstance(provider, ProvisioningPlane)


def test_eight_planes_present_and_allocation_absent() -> None:
    assert len(Plane) == 8
    assert not hasattr(interfaces, "AllocationPlane")
