"""Per-op cancel/compensation policy declaration (ADR-0072, M1.5 issue 3).

Each fault-inject op declares whether a ``jobs.cancel`` mid-flight yields clean-rollback,
best-effort, or orphan-flagged state — so "cancel is never undefined" is tested (issue 7)
against a declaration, not asserted in prose. These tests pin the declaration's totality
and the per-plane policy table.
"""

from __future__ import annotations

from kdive.providers.fault_inject.cancel_policy import (
    CANCEL_POLICY,
    CancelPolicy,
    cancel_policy_for,
)
from kdive.providers.fault_inject.engine import FaultPlane


def test_every_plane_declares_a_cancel_policy() -> None:
    # No op is left with an undefined cancel policy (the whole point of the declaration).
    assert set(CANCEL_POLICY) == set(FaultPlane)


def test_cancel_policy_for_returns_the_declared_policy_per_plane() -> None:
    expected = {
        FaultPlane.PROVISION: CancelPolicy.ORPHAN_FLAGGED,
        FaultPlane.INSTALL: CancelPolicy.BEST_EFFORT,
        FaultPlane.BOOT: CancelPolicy.BEST_EFFORT,
        FaultPlane.CONNECT: CancelPolicy.CLEAN_ROLLBACK,
        FaultPlane.CONTROL: CancelPolicy.CLEAN_ROLLBACK,
        FaultPlane.RETRIEVE: CancelPolicy.BEST_EFFORT,
    }
    for plane, policy in expected.items():
        assert cancel_policy_for(plane) is policy
