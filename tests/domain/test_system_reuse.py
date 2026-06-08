"""Unit tests for the system-reuse snapshot-≥ match helper (ADR-0070, #166)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, System
from kdive.domain.pcie import PCIeClaim
from kdive.domain.state import AllocationState, SystemState
from kdive.domain.system_reuse import (
    ReuseRequirement,
    read_system_sizing,
    snapshot_satisfies,
)
from kdive.profiles.provisioning import AllocationSizing

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _profile(*, vcpu: int = 4, memory_mb: int = 8192, disk_gb: int = 40) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": vcpu,
        "memory_mb": memory_mb,
        "disk_gb": disk_gb,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org#v6.9",
        "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": "/img"}}},
    }


def _alloc(
    *,
    vcpus: int | None = None,
    memory_gb: int | None = None,
    disk_gb: int | None = None,
    pcie_claim: list[PCIeClaim] | None = None,
) -> Allocation:
    return Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=uuid4(),
        state=AllocationState.ACTIVE,
        requested_vcpus=vcpus,
        requested_memory_gb=memory_gb,
        requested_disk_gb=disk_gb,
        pcie_claim=pcie_claim or [],
    )


def _system(profile: dict[str, object] | None = None) -> System:
    return System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile=profile if profile is not None else _profile(),
    )


# --- read_system_sizing ---------------------------------------------------------------


def test_read_sizing_prefers_allocation_snapshot_normalized_to_mb() -> None:
    alloc = _alloc(vcpus=8, memory_gb=16, disk_gb=100)
    system = _system(_profile(vcpu=2, memory_mb=2048, disk_gb=10))

    sizing = read_system_sizing(alloc, system)

    assert sizing == AllocationSizing(vcpu=8, memory_mb=16 * 1024, disk_gb=100)


def test_read_sizing_falls_back_to_profile_for_full_custom_allocation() -> None:
    # Full-custom: requested_* NULL -> size lives only in the provisioning_profile JSON.
    alloc = _alloc(vcpus=None, memory_gb=None, disk_gb=None)
    system = _system(_profile(vcpu=4, memory_mb=8192, disk_gb=40))

    sizing = read_system_sizing(alloc, system)

    assert sizing == AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)


def test_read_sizing_partial_allocation_snapshot_falls_back_to_profile() -> None:
    # Incomplete snapshot (any field NULL) is not authoritative -> use the profile.
    alloc = _alloc(vcpus=8, memory_gb=None, disk_gb=100)
    system = _system(_profile(vcpu=4, memory_mb=8192, disk_gb=40))

    sizing = read_system_sizing(alloc, system)

    assert sizing == AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)


# --- snapshot_satisfies: sizing -------------------------------------------------------


def test_snapshot_satisfies_exact_equal_sizing() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(vcpus=4, memory_gb=8, disk_gb=40)

    assert snapshot_satisfies(sizing, [], req) is True


def test_snapshot_satisfies_strictly_bigger_system_matches() -> None:
    sizing = AllocationSizing(vcpu=16, memory_mb=32 * 1024, disk_gb=200)
    req = ReuseRequirement(vcpus=4, memory_gb=8, disk_gb=40)

    assert snapshot_satisfies(sizing, [], req) is True


@pytest.mark.parametrize(
    "req",
    [
        ReuseRequirement(vcpus=8),  # vcpu short
        ReuseRequirement(memory_gb=16),  # memory short
        ReuseRequirement(disk_gb=80),  # disk short
    ],
)
def test_snapshot_smaller_on_any_axis_fails(req: ReuseRequirement) -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)

    assert snapshot_satisfies(sizing, [], req) is False


def test_snapshot_partial_requirement_only_checks_given_axes() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(vcpus=2)  # only vcpu asserted

    assert snapshot_satisfies(sizing, [], req) is True


# --- snapshot_satisfies: pcie ---------------------------------------------------------

_CLAIM: PCIeClaim = {"bdf": "0000:01:00.0", "vendor_id": "8086", "device_id": "1572"}


def test_snapshot_pcie_claim_contains_required_device() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(pcie=["8086:1572"])

    assert snapshot_satisfies(sizing, [_CLAIM], req) is True


def test_snapshot_pcie_missing_device_fails() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(pcie=["10de:1eb8"])

    assert snapshot_satisfies(sizing, [_CLAIM], req) is False


def test_snapshot_pcie_class_spec_is_configuration_error() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(pcie=["class=02"])

    with pytest.raises(CategorizedError) as exc:
        snapshot_satisfies(sizing, [_CLAIM], req)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_snapshot_malformed_pcie_spec_is_configuration_error() -> None:
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(pcie=["not-a-spec"])

    with pytest.raises(CategorizedError) as exc:
        snapshot_satisfies(sizing, [_CLAIM], req)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_snapshot_malformed_pcie_spec_raises_even_when_sizing_would_miss() -> None:
    # A malformed pcie spec surfaces its own error deterministically, not masked by a
    # sizing shortfall that would otherwise return False first.
    sizing = AllocationSizing(vcpu=4, memory_mb=8192, disk_gb=40)
    req = ReuseRequirement(vcpus=999, pcie=["class=02"])

    with pytest.raises(CategorizedError) as exc:
        snapshot_satisfies(sizing, [_CLAIM], req)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- ReuseRequirement.is_empty (require_pcie=[] no-op) --------------------------------


def test_requirement_is_empty_when_nothing_asserted() -> None:
    assert ReuseRequirement().is_empty() is True


def test_requirement_empty_pcie_list_is_no_op() -> None:
    # require_pcie=[] is "provided but asserts nothing" -> empty, no forced read/match.
    assert ReuseRequirement(pcie=[]).is_empty() is True


def test_requirement_with_any_axis_is_not_empty() -> None:
    assert ReuseRequirement(vcpus=1).is_empty() is False
    assert ReuseRequirement(pcie=["8086:1572"]).is_empty() is False
