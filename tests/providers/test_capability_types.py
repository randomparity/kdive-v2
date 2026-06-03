"""Tests for the capability value types (ADR-0022, issue #13)."""

from __future__ import annotations

import dataclasses

import pytest

from kdive.domain.models import ResourceKind
from kdive.providers.capability import (
    BoundOp,
    Capability,
    CleanupGuarantee,
    OpContract,
    Plane,
)


def _contract() -> OpContract:
    return OpContract(
        idempotent=True,
        destructive=False,
        cancelable=False,
        long_running=True,
        cleanup=CleanupGuarantee.BEST_EFFORT,
    )


def test_plane_enum_has_the_eight_provider_planes() -> None:
    assert {p.value for p in Plane} == {
        "discovery",
        "provisioning",
        "build",
        "install",
        "connect",
        "debug",
        "control",
        "retrieve",
    }
    assert len(Plane) == 8


def test_cleanup_guarantee_values() -> None:
    assert {c.value for c in CleanupGuarantee} == {
        "clean-rollback",
        "best-effort",
        "orphan-flagged",
    }


def test_opcontract_is_frozen() -> None:
    contract = _contract()
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.idempotent = False  # ty: ignore[invalid-assignment]  # prove frozen raises


def test_opcontract_and_capability_are_hashable() -> None:
    cap = Capability(
        plane=Plane.BUILD,
        operation="build",
        resource_kind=ResourceKind.LOCAL_LIBVIRT,
        contract=_contract(),
    )
    # Hashable → usable as a set member / dict key (registry key components).
    assert {cap, cap} == {cap}
    assert {_contract()} == {_contract()}


def test_malformed_cleanup_raises() -> None:
    with pytest.raises(TypeError):
        OpContract(
            idempotent=True,
            destructive=False,
            cancelable=False,
            long_running=False,
            cleanup="bogus",  # ty: ignore[invalid-argument-type]  # prove runtime guard
        )


def test_boundop_carries_contract_and_callable() -> None:
    contract = _contract()
    called: list[str] = []

    def fake_call() -> str:
        called.append("x")
        return "ok"

    bound = BoundOp(provider_id="p-1", operation="build", contract=contract, call=fake_call)
    assert bound.provider_id == "p-1"
    assert bound.contract is contract
    assert bound.call() == "ok"
    assert called == ["x"]
