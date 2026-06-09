"""Cancel/compensation semantics: cancel is never undefined (M1.5 #186, ADR-0072).

`jobs.cancel` lands **mid-op** on a half-done fault-inject op (the mock pauses via injected
latency, modeled here as a bounded wait on a :class:`CancelSignal`). Each op's **declared**
cancel policy (``cancel_policy.py``) is exercised and the distinct post-cancel state in the
mock infra inventory is asserted, so "cancel is never undefined" is proven behaviorally, not
in prose. The three states are distinguished by reconciler-visible inventory shape:

- ``CLEAN_ROLLBACK`` -> entry absent (nothing leaks);
- ``BEST_EFFORT`` -> entry present, unflagged (residue swept later);
- ``ORPHAN_FLAGGED`` -> entry present, flagged (left deliberately for the reaper).

A best-effort domain's inventory shape equals a healthy uncancelled domain's, so a
best-effort test must also assert the returned :class:`CancelOutcome` to prove a cancel
actually landed (the inventory alone cannot).
"""

from __future__ import annotations

import threading
from uuid import UUID

import pytest

from kdive.providers.fault_inject.cancel import (
    CancelOutcome,
    CancelSignal,
    run_cancelable_op,
)
from kdive.providers.fault_inject.cancel_policy import CancelPolicy, cancel_policy_for
from kdive.providers.fault_inject.engine import FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory

_SYSTEM = UUID("33333333-3333-3333-3333-333333333333")
_DOMAIN = "fault-inject-domain-186"

# A wait long enough that the runner is paused inside it when the test sets the signal, yet
# short enough to keep CI fast even if a cancel never lands (the happy-path/no-cancel cases).
_LATENCY_S = 0.2


def _record_domain(inventory: FaultInjectInventory) -> str:
    """The op's first observable side effect: mint and record the synthetic domain."""
    inventory.record(_SYSTEM, _DOMAIN)
    return _DOMAIN


def _run_cancelled_op(inventory: FaultInjectInventory, plane: FaultPlane) -> CancelOutcome:
    """Drive ``plane``'s op to a deterministic mid-op cancel and return its outcome.

    The signal is pre-set, so ``run_cancelable_op`` records the domain (its first side
    effect) and then observes the cancel **immediately** at the wait checkpoint — no
    wall-clock race between two sleeps. "Mid-op" holds because the side effect runs inside
    the runner before the wait regardless of when the signal was set; pre-setting only
    removes the nondeterministic timing (ADR-0072: deterministic CI, not flaky soak).
    """
    signal = CancelSignal()
    signal.cancel()
    return run_cancelable_op(
        signal=signal,
        plane=plane,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )


# --- Step 2: CancelSignal -----------------------------------------------------------------


def test_wait_for_cancel_returns_true_when_signalled_from_another_thread() -> None:
    signal = CancelSignal()
    threading.Thread(target=signal.cancel).start()

    # The signal is set promptly from another thread; the wait observes it well within bound.
    assert signal.wait_for_cancel(timeout_s=5.0) is True


def test_wait_for_cancel_returns_false_when_the_timeout_elapses() -> None:
    signal = CancelSignal()

    assert signal.wait_for_cancel(timeout_s=0.01) is False


def test_cancel_is_idempotent() -> None:
    signal = CancelSignal()

    signal.cancel()
    signal.cancel()

    assert signal.wait_for_cancel(timeout_s=0.0) is True


def test_wait_for_cancel_with_nonpositive_timeout_does_not_block() -> None:
    signal = CancelSignal()

    # A zero/negative timeout is an immediate poll, not an indefinite block.
    assert signal.wait_for_cancel(timeout_s=0.0) is False
    assert signal.wait_for_cancel(timeout_s=-1.0) is False


# --- Step 3: happy path (no cancel) -------------------------------------------------------


def test_no_cancel_completes_and_leaves_the_normal_inventory_state() -> None:
    inventory = FaultInjectInventory()
    signal = CancelSignal()  # never set

    outcome = run_cancelable_op(
        signal=signal,
        plane=FaultPlane.PROVISION,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )

    assert outcome.observed_cancel is False
    assert outcome.result == _DOMAIN
    # Happy path: the domain is recorded and unflagged — the exact shape best-effort shares,
    # so the two are told apart by ``observed_cancel``, never by inventory alone.
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    assert inventory.is_orphaned(_DOMAIN) is False


# --- Step 4: ORPHAN_FLAGGED (provision) ---------------------------------------------------


def test_provision_cancel_leaves_an_orphan_flagged_entry() -> None:
    inventory = FaultInjectInventory()

    outcome = _run_cancelled_op(inventory, FaultPlane.PROVISION)

    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.ORPHAN_FLAGGED
    # Orphan-flagged: the entry stays for the reaper, flagged as a deliberate cancel residue.
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    assert inventory.is_orphaned(_DOMAIN) is True


# --- Step 5: BEST_EFFORT (install) --------------------------------------------------------


def test_install_cancel_leaves_unflagged_residue_proven_by_the_outcome() -> None:
    inventory = FaultInjectInventory()

    outcome = _run_cancelled_op(inventory, FaultPlane.INSTALL)

    # The inventory shape (present, unflagged) equals a healthy domain's, so the cancel is
    # proven by the outcome, not the inventory.
    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.BEST_EFFORT
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    assert inventory.is_orphaned(_DOMAIN) is False


# --- Step 6: CLEAN_ROLLBACK (connect) -----------------------------------------------------


def test_connect_cancel_fully_rolls_back_leaving_nothing() -> None:
    inventory = FaultInjectInventory()

    outcome = _run_cancelled_op(inventory, FaultPlane.CONNECT)

    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.CLEAN_ROLLBACK
    # Clean rollback: nothing leaks — the partial domain is fully unwound.
    assert inventory.owned_domains() == []
    assert inventory.is_orphaned(_DOMAIN) is False


# --- Cancel lands strictly after the side effect (deterministic ordering) ------------------


def test_cancel_is_observed_only_after_the_op_records_its_side_effect() -> None:
    """A cancel set *while the op runs its side effect* is observed mid-op, not before it.

    Driven by a latch off ``domain_op`` (not paired sleeps): the side effect blocks until
    the test has fired the cancel, then the runner reaches its wait and observes it. This
    proves the runner records the partial state *before* the wait checkpoint — the "half-done
    op" the issue requires — deterministically.
    """
    inventory = FaultInjectInventory()
    signal = CancelSignal()
    in_side_effect = threading.Event()
    may_finish_side_effect = threading.Event()

    def _op() -> str:
        domain = _record_domain(inventory)
        in_side_effect.set()
        # Hold inside the side effect until the test has landed the cancel, so the cancel
        # provably lands on an op that has already recorded its partial state.
        assert may_finish_side_effect.wait(timeout=5.0)
        return domain

    result: dict[str, CancelOutcome] = {}

    def _drive() -> None:
        result["outcome"] = run_cancelable_op(
            signal=signal,
            plane=FaultPlane.PROVISION,
            domain_op=_op,
            inventory=inventory,
            latency_s=_LATENCY_S,
        )

    runner = threading.Thread(target=_drive)
    runner.start()
    assert in_side_effect.wait(timeout=5.0)  # the op has recorded its domain, mid-op
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    signal.cancel()  # land the cancel on the half-done op
    may_finish_side_effect.set()
    runner.join(timeout=5.0)
    assert not runner.is_alive()

    outcome = result["outcome"]
    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.ORPHAN_FLAGGED
    assert inventory.is_orphaned(_DOMAIN) is True


# --- Step 7: behavioral totality ----------------------------------------------------------


@pytest.mark.parametrize("plane", list(FaultPlane))
def test_every_plane_has_a_defined_post_cancel_state(plane: FaultPlane) -> None:
    inventory = FaultInjectInventory()

    outcome = _run_cancelled_op(inventory, plane)

    # No plane is left with an undefined compensation: the applied policy equals the
    # declaration, and the inventory shape is that policy's distinct, defined shape.
    expected = cancel_policy_for(plane)
    assert outcome.observed_cancel is True
    assert outcome.policy is expected
    present = {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    if expected is CancelPolicy.CLEAN_ROLLBACK:
        assert not present
        assert inventory.is_orphaned(_DOMAIN) is False
    elif expected is CancelPolicy.BEST_EFFORT:
        assert present
        assert inventory.is_orphaned(_DOMAIN) is False
    else:
        assert expected is CancelPolicy.ORPHAN_FLAGGED
        assert present
        assert inventory.is_orphaned(_DOMAIN) is True
