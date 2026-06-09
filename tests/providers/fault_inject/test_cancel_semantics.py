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

from kdive.providers.fault_inject.cancel import CancelSignal, run_cancelable_op
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


def _cancel_during_wait(signal: CancelSignal) -> threading.Thread:
    """Set the cancel signal shortly after the runner enters its wait window."""
    thread = threading.Thread(target=lambda: (threading.Event().wait(0.02), signal.cancel()))
    thread.start()
    return thread


# --- Step 4: ORPHAN_FLAGGED (provision) ---------------------------------------------------


def test_provision_cancel_leaves_an_orphan_flagged_entry() -> None:
    inventory = FaultInjectInventory()
    signal = CancelSignal()
    waiter = _cancel_during_wait(signal)

    outcome = run_cancelable_op(
        signal=signal,
        plane=FaultPlane.PROVISION,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )
    waiter.join()

    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.ORPHAN_FLAGGED
    # Orphan-flagged: the entry stays for the reaper, flagged as a deliberate cancel residue.
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    assert inventory.is_orphaned(_DOMAIN) is True


# --- Step 5: BEST_EFFORT (install) --------------------------------------------------------


def test_install_cancel_leaves_unflagged_residue_proven_by_the_outcome() -> None:
    inventory = FaultInjectInventory()
    signal = CancelSignal()
    waiter = _cancel_during_wait(signal)

    outcome = run_cancelable_op(
        signal=signal,
        plane=FaultPlane.INSTALL,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )
    waiter.join()

    # The inventory shape (present, unflagged) equals a healthy domain's, so the cancel is
    # proven by the outcome, not the inventory.
    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.BEST_EFFORT
    assert {d.name for d in inventory.owned_domains()} == {_DOMAIN}
    assert inventory.is_orphaned(_DOMAIN) is False


# --- Step 6: CLEAN_ROLLBACK (connect) -----------------------------------------------------


def test_connect_cancel_fully_rolls_back_leaving_nothing() -> None:
    inventory = FaultInjectInventory()
    signal = CancelSignal()
    waiter = _cancel_during_wait(signal)

    outcome = run_cancelable_op(
        signal=signal,
        plane=FaultPlane.CONNECT,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )
    waiter.join()

    assert outcome.observed_cancel is True
    assert outcome.policy is CancelPolicy.CLEAN_ROLLBACK
    # Clean rollback: nothing leaks — the partial domain is fully unwound.
    assert inventory.owned_domains() == []
    assert inventory.is_orphaned(_DOMAIN) is False


# --- Step 7: behavioral totality ----------------------------------------------------------


@pytest.mark.parametrize("plane", list(FaultPlane))
def test_every_plane_has_a_defined_post_cancel_state(plane: FaultPlane) -> None:
    inventory = FaultInjectInventory()
    signal = CancelSignal()
    waiter = _cancel_during_wait(signal)

    outcome = run_cancelable_op(
        signal=signal,
        plane=plane,
        domain_op=lambda: _record_domain(inventory),
        inventory=inventory,
        latency_s=_LATENCY_S,
    )
    waiter.join()

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
