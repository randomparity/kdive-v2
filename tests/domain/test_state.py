"""Tests for the M0 object lifecycles (`kdive.domain.state`).

The legal edges below are transcribed by hand from the spec's "Domain objects in
M0" table so the tests check behavior against the spec rather than against the
guard table they exercise.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum

import pytest

from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    IllegalTransition,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
    can_transition,
    ensure_transition,
)

# Each object's legal forward edges, straight from the spec. Terminal states map
# to an empty set. Resource health flips freely between its three values.
LEGAL: dict[type[StrEnum], dict[StrEnum, set[StrEnum]]] = {
    ResourceStatus: {
        ResourceStatus.AVAILABLE: {ResourceStatus.DEGRADED, ResourceStatus.OFFLINE},
        ResourceStatus.DEGRADED: {ResourceStatus.AVAILABLE, ResourceStatus.OFFLINE},
        ResourceStatus.OFFLINE: {ResourceStatus.AVAILABLE, ResourceStatus.DEGRADED},
    },
    AllocationState: {
        AllocationState.REQUESTED: {AllocationState.GRANTED, AllocationState.FAILED},
        AllocationState.GRANTED: {
            AllocationState.ACTIVE,
            AllocationState.RELEASING,
            AllocationState.EXPIRED,
            AllocationState.FAILED,
        },
        AllocationState.ACTIVE: {
            AllocationState.RELEASING,
            AllocationState.EXPIRED,
            AllocationState.FAILED,
        },
        AllocationState.RELEASING: {AllocationState.RELEASED, AllocationState.FAILED},
        AllocationState.RELEASED: set(),
        AllocationState.EXPIRED: set(),
        AllocationState.FAILED: set(),
    },
    SystemState: {
        SystemState.DEFINED: {
            SystemState.PROVISIONING,
            SystemState.TORN_DOWN,
            SystemState.FAILED,
        },
        SystemState.PROVISIONING: {
            SystemState.READY,
            SystemState.FAILED,
            SystemState.TORN_DOWN,
        },
        SystemState.READY: {
            SystemState.CRASHED,
            SystemState.TORN_DOWN,
            SystemState.REPROVISIONING,
            SystemState.FAILED,
        },
        SystemState.REPROVISIONING: {SystemState.READY, SystemState.FAILED},
        SystemState.CRASHED: {SystemState.TORN_DOWN, SystemState.FAILED},
        SystemState.TORN_DOWN: set(),
        SystemState.FAILED: set(),
    },
    InvestigationState: {
        InvestigationState.OPEN: {
            InvestigationState.ACTIVE,
            InvestigationState.CLOSED,
            InvestigationState.ABANDONED,
        },
        InvestigationState.ACTIVE: {InvestigationState.CLOSED, InvestigationState.ABANDONED},
        InvestigationState.CLOSED: set(),
        InvestigationState.ABANDONED: set(),
    },
    RunState: {
        RunState.CREATED: {RunState.RUNNING, RunState.CANCELED},
        RunState.RUNNING: {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELED},
        RunState.SUCCEEDED: set(),
        RunState.FAILED: set(),
        RunState.CANCELED: set(),
    },
    DebugSessionState: {
        DebugSessionState.ATTACH: {DebugSessionState.LIVE, DebugSessionState.DETACHED},
        DebugSessionState.LIVE: {DebugSessionState.DETACHED},
        DebugSessionState.DETACHED: set(),
    },
    JobState: {
        JobState.QUEUED: {JobState.RUNNING, JobState.CANCELED},
        JobState.RUNNING: {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELED,
            JobState.QUEUED,
        },
        JobState.SUCCEEDED: set(),
        JobState.FAILED: set(),
        JobState.CANCELED: set(),
    },
}


def _legal_edges() -> Iterator[tuple[StrEnum, StrEnum]]:
    for table in LEGAL.values():
        for frm, tos in table.items():
            for to in tos:
                yield frm, to


def _illegal_edges() -> Iterator[tuple[StrEnum, StrEnum]]:
    # Every same-type pair that is not a legal edge and is not a self-loop.
    for table in LEGAL.values():
        states = list(table)
        for frm in states:
            for to in states:
                if frm is not to and to not in table[frm]:
                    yield frm, to


@pytest.mark.parametrize(("frm", "to"), list(_legal_edges()), ids=lambda v: v.name)
def test_legal_transitions_are_allowed(frm: StrEnum, to: StrEnum) -> None:
    assert can_transition(frm, to) is True
    ensure_transition(frm, to)  # must not raise


@pytest.mark.parametrize(("frm", "to"), list(_illegal_edges()), ids=lambda v: v.name)
def test_illegal_transitions_are_rejected(frm: StrEnum, to: StrEnum) -> None:
    assert can_transition(frm, to) is False
    with pytest.raises(IllegalTransition):
        ensure_transition(frm, to)


@pytest.mark.parametrize("enum_cls", list(LEGAL), ids=lambda c: c.__name__)
def test_self_transitions_are_not_allowed(enum_cls: type[StrEnum]) -> None:
    for state in LEGAL[enum_cls]:
        assert can_transition(state, state) is False


@pytest.mark.parametrize("enum_cls", list(LEGAL), ids=lambda c: c.__name__)
def test_every_lifecycle_member_is_covered_by_the_guard_table(enum_cls: type[StrEnum]) -> None:
    # A member missing from the implementation's table makes can_transition raise
    # TypeError; iterating the enum itself (the source of truth) locks the table to
    # stay complete as the enums grow.
    for member in enum_cls.__members__.values():
        assert can_transition(member, member) is False


def test_representative_illegal_transition_raises_with_context() -> None:
    # A terminal state has no exits; attempting one is the canonical illegal move.
    with pytest.raises(IllegalTransition, match="torn_down"):
        ensure_transition(SystemState.TORN_DOWN, SystemState.READY)


def test_mixing_two_object_enums_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        can_transition(SystemState.READY, RunState.RUNNING)


def test_allocation_expiry_edges_are_legal_from_granted_and_active() -> None:
    # M1 reconciler →expired sweep reaches `expired` from a granted-but-unprovisioned
    # allocation and from an active one; `expired` is terminal.
    assert can_transition(AllocationState.GRANTED, AllocationState.EXPIRED) is True
    assert can_transition(AllocationState.ACTIVE, AllocationState.EXPIRED) is True
    assert can_transition(AllocationState.RELEASING, AllocationState.EXPIRED) is False
    with pytest.raises(IllegalTransition, match="expired"):
        ensure_transition(AllocationState.EXPIRED, AllocationState.RELEASED)


def test_system_reprovision_cycle_edges_are_legal() -> None:
    # M1 reprovision-in-place: ready ↔ reprovisioning, reprovisioning → failed.
    assert can_transition(SystemState.READY, SystemState.REPROVISIONING) is True
    assert can_transition(SystemState.REPROVISIONING, SystemState.READY) is True
    assert can_transition(SystemState.REPROVISIONING, SystemState.FAILED) is True
    # Reprovisioning cannot jump straight to teardown without returning to ready.
    assert can_transition(SystemState.REPROVISIONING, SystemState.TORN_DOWN) is False


def test_force_crash_edge_is_legal_teardown_skip_is_not() -> None:
    # The spec's force_crash path: ready -> crashed -> torn_down.
    assert can_transition(SystemState.READY, SystemState.CRASHED) is True
    assert can_transition(SystemState.CRASHED, SystemState.TORN_DOWN) is True
    # A System cannot un-crash back to ready.
    assert can_transition(SystemState.CRASHED, SystemState.READY) is False
