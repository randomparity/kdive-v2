"""Per-operation cancel and compensation policy declarations (ADR-0072).

Every fault-inject operation declares whether a ``jobs.cancel`` landing mid-flight yields
clean rollback, best-effort cleanup, or orphan-flagged state. Tests use this table as the
source of truth for cancel behavior; this module owns only the declaration, while
``engine.py`` owns fault draws and latency injection.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from kdive.providers.fault_inject.faulting.engine import FaultPlane


class CancelPolicy(StrEnum):
    """What a ``jobs.cancel`` landing mid-op leaves behind for an op.

    Attributes:
        CLEAN_ROLLBACK: The op's partial state is fully unwound on cancel — nothing leaks.
        BEST_EFFORT: The op unwinds what it can; residue is tolerated and swept later.
        ORPHAN_FLAGGED: The op may leave provider state the reconciler reaps (it flags rather
            than guarantees rollback).
    """

    CLEAN_ROLLBACK = "clean_rollback"
    BEST_EFFORT = "best_effort"
    ORPHAN_FLAGGED = "orphan_flagged"


# Each op's declared cancel policy, derived from its compensation reality:
# - provision may leave a half-minted domain the reconciler leaked-domain pass reaps.
# - install/boot tear down side effects best-effort; residue is swept by reconciliation.
# - connect/control are idempotent/atomic — a mid-op cancel rolls back cleanly.
# - retrieve drops a partial capture artifact best-effort.
CANCEL_POLICY: Final[dict[FaultPlane, CancelPolicy]] = {
    FaultPlane.PROVISION: CancelPolicy.ORPHAN_FLAGGED,
    FaultPlane.INSTALL: CancelPolicy.BEST_EFFORT,
    FaultPlane.BOOT: CancelPolicy.BEST_EFFORT,
    FaultPlane.CONNECT: CancelPolicy.CLEAN_ROLLBACK,
    FaultPlane.CONTROL: CancelPolicy.CLEAN_ROLLBACK,
    FaultPlane.RETRIEVE: CancelPolicy.BEST_EFFORT,
}


def cancel_policy_for(plane: FaultPlane) -> CancelPolicy:
    """Return the declared cancel policy for a plane (every plane has one — a totality test
    asserts no op is left undefined).
    """
    return CANCEL_POLICY[plane]
