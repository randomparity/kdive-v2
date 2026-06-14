"""System admission service helper tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState
from kdive.services.systems import admission
from tests.mcp.systems_support import provisioning_profile

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_ALLOC_ID = UUID("00000000-0000-0000-0000-00000000ad01")


def _allocation(
    *,
    vcpus: int | None = 4,
    memory_gb: int | None = 8,
    disk_gb: int | None = 40,
) -> Allocation:
    return Allocation(
        id=_ALLOC_ID,
        created_at=_DT,
        updated_at=_DT,
        principal="agent",
        agent_session="sess",
        project="proj",
        state=AllocationState.GRANTED,
        requested_vcpus=vcpus,
        requested_memory_gb=memory_gb,
        requested_disk_gb=disk_gb,
        shape="medium",
    )


def _profile(**sizing: int) -> dict[str, object]:
    data = provisioning_profile()
    for key in ("vcpu", "memory_mb", "disk_gb"):
        data.pop(key, None)
    data.update(sizing)
    return data


def test_failure_from_error_keeps_only_json_safe_scalar_details() -> None:
    exc = CategorizedError(
        "bad profile",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "path": "/tmp/rootfs.qcow2",
            "ok": True,
            "count": 3,
            "ratio": 1.5,
            "nan": float("nan"),
            "nested": {"drop": "me"},
        },
    )

    failure = admission._failure_from_error("object-1", exc)

    assert failure.category is ErrorCategory.CONFIGURATION_ERROR
    assert failure.data == {
        "path": "/tmp/rootfs.qcow2",
        "ok": True,
        "count": 3,
        "ratio": 1.5,
    }


def test_stored_profile_fills_sizing_from_allocation_snapshot() -> None:
    stored = admission._stored_profile_for(_profile(), _allocation())

    assert stored.vcpu == 4
    assert stored.memory_mb == 8192
    assert stored.disk_gb == 40


def test_stored_profile_rejects_conflicting_allocation_sizing_restatement() -> None:
    with pytest.raises(CategorizedError) as exc:
        admission._stored_profile_for(_profile(vcpu=8), _allocation())

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_stored_profile_requires_concrete_sizing_without_allocation_snapshot() -> None:
    with pytest.raises(CategorizedError) as exc:
        admission._stored_profile_for(
            _profile(),
            _allocation(vcpus=None, memory_gb=None, disk_gb=None),
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
