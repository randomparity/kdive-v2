"""Co-located ``KDIVE_FAULT_INJECT_*`` settings for the fault-inject provider (ADR-0087).

A dependency-light module (imports only :class:`Setting`). The provider-enable gate
``KDIVE_FAULT_INJECT`` is core (read by ``composition`` without importing the provider);
these are the provider's own knobs.
"""

from __future__ import annotations

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})

FAULT_INJECT_URI = Setting(
    name="KDIVE_FAULT_INJECT_URI",
    parse=str,
    default="fault-inject://local",
    group="fault-inject",
    processes=_RT,
    help="Synthetic host URI for the fault-inject resource.",
)
FAULT_INJECT_ALLOCATION_CAP = Setting(
    name="KDIVE_FAULT_INJECT_ALLOCATION_CAP",
    parse=str,
    default="1",
    group="fault-inject",
    processes=_RT,
    help="Per-plane concurrent-Allocation cap.",
)
FAULT_INJECT_SEED = Setting(
    name="KDIVE_FAULT_INJECT_SEED",
    parse=str,
    default="0",
    group="fault-inject",
    processes=_RT,
    help="Deterministic fault-engine seed.",
)
FAULT_INJECT_SECRET_REF = Setting(
    name="KDIVE_FAULT_INJECT_SECRET_REF",  # pragma: allowlist secret - env var name
    parse=str,
    default="fault-inject/console-sentinel",
    secret=True,
    group="fault-inject",
    processes=_RT,
    help="Secret reference for the console-sentinel probe.",
)

SETTINGS = [
    FAULT_INJECT_URI,
    FAULT_INJECT_ALLOCATION_CAP,
    FAULT_INJECT_SEED,
    FAULT_INJECT_SECRET_REF,
]
