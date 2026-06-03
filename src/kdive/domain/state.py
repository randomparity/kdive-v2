"""M0 object lifecycles and the transition guard (ADR-0003).

A :class:`~enum.StrEnum` per durable object plus :func:`can_transition`, the guard
the repository layer consults before persisting a state change. The legal edges
encode the M0-subset state machines from ``m0-walking-skeleton.md`` ("Domain
objects in M0").

Two readings of that table are pinned here because its notation is ambiguous:

* **System ``ready`` is not terminal.** The table bolds ``ready`` but the prose,
  the walking-skeleton path, and ``force_crash`` all transition out of it
  (``ready → crashed`` and ``ready → torn_down``); only ``torn_down`` and
  ``failed`` are terminal.
* **DebugSession is forward-only in M0.** The spec draws ``attach ↔ live ↔
  detached`` conceptually, but no M0 tool reattaches or steps backward, and the
  session "ends at reboot/crash" — so M0 drives ``attach → live → detached`` with
  ``detached`` terminal. ``attach → detached`` is also legal: a failed attach
  aborts straight to the terminal rather than stranding the row in ``attach``
  (no reconciler rule sweeps a stuck ``attach``). Reattach returns when M1 needs it.

``failed`` is reachable from every non-terminal state of the objects that carry
it. Resource health (``available``/``degraded``/``offline``) is not a lifecycle —
it flips freely between its three values.
"""

from __future__ import annotations

from enum import StrEnum


class ResourceStatus(StrEnum):
    """Health of a registered resource host (free transitions among the three)."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AllocationState(StrEnum):
    """Always-yes, capacity-checked allocation lifecycle."""

    REQUESTED = "requested"
    GRANTED = "granted"
    ACTIVE = "active"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"


class SystemState(StrEnum):
    """One System per Allocation in M0 (no reprovision)."""

    DEFINED = "defined"
    PROVISIONING = "provisioning"
    READY = "ready"
    CRASHED = "crashed"
    TORN_DOWN = "torn_down"
    FAILED = "failed"


class InvestigationState(StrEnum):
    """Project-scoped campaign; becomes ``active`` on its first Run."""

    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class RunState(StrEnum):
    """One build per Run; a failed step is terminal for the Run."""

    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class DebugSessionState(StrEnum):
    """One boot = one session; ends at reboot/crash (``detached``)."""

    ATTACH = "attach"
    LIVE = "live"
    DETACHED = "detached"


class JobState(StrEnum):
    """Durable job lifecycle; ``running → queued`` is a bounded-retry requeue."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class IllegalTransition(ValueError):
    """Raised when a state change is not permitted by the M0 guard table.

    A programming/invariant error, distinct from the operational failures in
    :class:`kdive.domain.errors.ErrorCategory`.
    """


# Adjacency nested by enum class: each member maps to its allowed successors
# (terminals -> empty). StrEnum members hash by their string value, so members of
# different enums that share a value (e.g. several `"failed"`) collide in a single
# dict; nesting by the class object (hashed by identity) keeps each lifecycle's
# table isolated.
_TRANSITIONS: dict[type[StrEnum], dict[StrEnum, frozenset[StrEnum]]] = {
    ResourceStatus: {
        ResourceStatus.AVAILABLE: frozenset({ResourceStatus.DEGRADED, ResourceStatus.OFFLINE}),
        ResourceStatus.DEGRADED: frozenset({ResourceStatus.AVAILABLE, ResourceStatus.OFFLINE}),
        ResourceStatus.OFFLINE: frozenset({ResourceStatus.AVAILABLE, ResourceStatus.DEGRADED}),
    },
    AllocationState: {
        AllocationState.REQUESTED: frozenset({AllocationState.GRANTED, AllocationState.FAILED}),
        AllocationState.GRANTED: frozenset({AllocationState.ACTIVE, AllocationState.FAILED}),
        AllocationState.ACTIVE: frozenset({AllocationState.RELEASING, AllocationState.FAILED}),
        AllocationState.RELEASING: frozenset({AllocationState.RELEASED, AllocationState.FAILED}),
        AllocationState.RELEASED: frozenset(),
        AllocationState.FAILED: frozenset(),
    },
    SystemState: {
        SystemState.DEFINED: frozenset({SystemState.PROVISIONING, SystemState.FAILED}),
        SystemState.PROVISIONING: frozenset({SystemState.READY, SystemState.FAILED}),
        SystemState.READY: frozenset(
            {SystemState.CRASHED, SystemState.TORN_DOWN, SystemState.FAILED}
        ),
        SystemState.CRASHED: frozenset({SystemState.TORN_DOWN, SystemState.FAILED}),
        SystemState.TORN_DOWN: frozenset(),
        SystemState.FAILED: frozenset(),
    },
    InvestigationState: {
        InvestigationState.OPEN: frozenset(
            {InvestigationState.ACTIVE, InvestigationState.CLOSED, InvestigationState.ABANDONED}
        ),
        InvestigationState.ACTIVE: frozenset(
            {InvestigationState.CLOSED, InvestigationState.ABANDONED}
        ),
        InvestigationState.CLOSED: frozenset(),
        InvestigationState.ABANDONED: frozenset(),
    },
    RunState: {
        RunState.CREATED: frozenset({RunState.RUNNING, RunState.CANCELED}),
        RunState.RUNNING: frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELED}),
        RunState.SUCCEEDED: frozenset(),
        RunState.FAILED: frozenset(),
        RunState.CANCELED: frozenset(),
    },
    DebugSessionState: {
        DebugSessionState.ATTACH: frozenset({DebugSessionState.LIVE, DebugSessionState.DETACHED}),
        DebugSessionState.LIVE: frozenset({DebugSessionState.DETACHED}),
        DebugSessionState.DETACHED: frozenset(),
    },
    JobState: {
        JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELED}),
        JobState.RUNNING: frozenset(
            {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED, JobState.QUEUED}
        ),
        JobState.SUCCEEDED: frozenset(),
        JobState.FAILED: frozenset(),
        JobState.CANCELED: frozenset(),
    },
}


def can_transition(frm: StrEnum, to: StrEnum) -> bool:
    """Report whether ``frm → to`` is a legal M0 transition.

    Args:
        frm: The current state.
        to: The proposed next state. Must be a member of the same enum as ``frm``.

    Returns:
        ``True`` if the edge is in the guard table; ``False`` otherwise (including
        self-transitions, which are never legal).

    Raises:
        TypeError: If ``frm`` and ``to`` are different enums, or ``frm`` is not a
            known lifecycle state — both signal a caller bug, not a denied
            transition.
    """
    if type(frm) is not type(to):
        raise TypeError(
            f"cannot compare states across {type(frm).__name__} and {type(to).__name__}"
        )
    table = _TRANSITIONS.get(type(frm))
    if table is None:
        raise TypeError(f"{type(frm).__name__} is not a known lifecycle")
    successors = table.get(frm)
    if successors is None:
        raise TypeError(f"{type(frm).__name__}.{frm.name} is not a known lifecycle state")
    return to in successors


def ensure_transition(frm: StrEnum, to: StrEnum) -> None:
    """Assert ``frm → to`` is legal, raising :class:`IllegalTransition` if not.

    Args:
        frm: The current state.
        to: The proposed next state.

    Raises:
        IllegalTransition: If the transition is not permitted.
        TypeError: Propagated from :func:`can_transition` for cross-enum or
            unknown-state misuse.
    """
    if not can_transition(frm, to):
        raise IllegalTransition(f"illegal {type(frm).__name__} transition: {frm} -> {to}")
