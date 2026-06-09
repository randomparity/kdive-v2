"""Mid-op cancel/compensation for the fault-inject mock (M1.5 #186, ADR-0072).

"Cancel is never undefined" is proven against a provider that can **pause mid-op on
demand**. A fault-inject op runs its first observable side effect (record the synthetic
domain in the inventory), then **pauses** on a bounded wait — the injected latency ADR-0072
names as the cancel lever. ``jobs.cancel`` lands inside that pause by setting a
:class:`CancelSignal`; the op observes the cancel cooperatively and applies the op's
**declared** :class:`~kdive.providers.fault_inject.cancel_policy.CancelPolicy`, leaving a
distinct, reconciler-visible inventory state per policy:

- ``CLEAN_ROLLBACK`` -> the entry is removed; nothing leaks.
- ``BEST_EFFORT`` -> the entry is left, unflagged; the leaked-domain reconciler pass reaps
  it later (tolerated residue, "swept later").
- ``ORPHAN_FLAGGED`` -> the entry is left and flagged, so a reaper/operator can tell a
  deliberate cancel residue apart from best-effort residue.

This seam is provider-local and driven directly with an injected signal (ADR-0019); it does
not import the worker or the MCP layer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from kdive.providers.fault_inject.cancel_policy import CancelPolicy, cancel_policy_for
from kdive.providers.fault_inject.engine import FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory


class CancelSignal:
    """A cooperative, thread-safe cancel signal an op polls while paused mid-flight.

    The fault-inject op runs in a worker thread (provider ports run under
    ``asyncio.to_thread``), so the signal wraps a :class:`threading.Event` rather than an
    asyncio primitive. ``jobs.cancel`` (or a test) calls :meth:`cancel` to land a cancel
    inside the op's bounded :meth:`wait_for_cancel` pause.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation (idempotent — a repeat is a no-op)."""
        self._event.set()

    def wait_for_cancel(self, *, timeout_s: float) -> bool:
        """Block up to ``timeout_s`` for a cancel; return whether one arrived.

        A non-positive ``timeout_s`` is an immediate poll, never an indefinite block:
        :meth:`threading.Event.wait` treats a non-positive timeout as "do not block."

        Returns:
            ``True`` if cancellation was requested within the window, else ``False``.
        """
        return self._event.wait(timeout_s)


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """The result of one cancelable op: whether a cancel landed and the policy applied.

    A best-effort cancel leaves the same inventory shape as a healthy uncancelled domain, so
    ``observed_cancel`` is the load-bearing signal that a mid-op cancel actually happened —
    the inventory state alone cannot prove it.

    Attributes:
        observed_cancel: Whether a ``jobs.cancel`` was observed mid-op (vs. the op
            completing normally).
        policy: The declared :class:`CancelPolicy` applied when ``observed_cancel``; ``None``
            when the op completed without a cancel.
        result: The op's own result (the synthetic domain name) when it completed; ``None``
            when a cancel pre-empted completion.
    """

    observed_cancel: bool
    policy: CancelPolicy | None
    result: str | None


def run_cancelable_op(
    *,
    signal: CancelSignal,
    plane: FaultPlane,
    domain_op: Callable[[], str],
    inventory: FaultInjectInventory,
    latency_s: float,
) -> CancelOutcome:
    """Run one fault-inject op that can be cancelled mid-flight, applying its cancel policy.

    The op performs its first observable side effect (``domain_op`` records the synthetic
    domain), then pauses on ``signal`` for up to ``latency_s`` — the injected-latency window
    a ``jobs.cancel`` lands inside. If a cancel arrives, the op compensates per
    :func:`cancel_policy_for` and returns an observed-cancel outcome; otherwise it completes
    and returns the op's result.

    Args:
        signal: The cooperative cancel signal the op polls during its pause.
        plane: The op's plane; selects the declared cancel policy.
        domain_op: The op's first side effect; returns the recorded synthetic domain name.
        inventory: The mock infra inventory the compensation mutates.
        latency_s: The injected-latency pause bound (seconds) the cancel can land within.

    Returns:
        A :class:`CancelOutcome` recording whether a cancel landed and the policy applied.
    """
    domain = domain_op()
    if not signal.wait_for_cancel(timeout_s=latency_s):
        return CancelOutcome(observed_cancel=False, policy=None, result=domain)
    policy = cancel_policy_for(plane)
    _compensate(policy, inventory, domain)
    return CancelOutcome(observed_cancel=True, policy=policy, result=None)


def _compensate(policy: CancelPolicy, inventory: FaultInjectInventory, domain: str) -> None:
    """Apply ``policy``'s compensation to ``domain`` in the mock inventory.

    ``BEST_EFFORT`` leaves the entry untouched as tolerated residue for the later
    leaked-domain sweep (the entry itself is the residue), so it needs no inventory mutation.

    Raises:
        ValueError: If ``policy`` is an unhandled :class:`CancelPolicy` member — a new policy
            must declare its compensation here rather than silently leaving undefined state.
    """
    if policy is CancelPolicy.CLEAN_ROLLBACK:
        inventory.forget(domain)
    elif policy is CancelPolicy.ORPHAN_FLAGGED:
        inventory.flag_orphan(domain)
    elif policy is not CancelPolicy.BEST_EFFORT:
        raise ValueError(f"unhandled cancel policy {policy!r}: no compensation defined")
