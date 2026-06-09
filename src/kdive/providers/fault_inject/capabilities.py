"""Fault-inject resource capability keys (ADR-0072).

The fault-inject resource's ``seed``, per-plane ``fault_rate`` map, per-plane
``max_latency_s`` bound, and ``secret_ref`` are **keys in the existing
``resources.capabilities`` jsonb** — no new columns (migration 0018 only widens the
``resources_kind_check`` CHECK). Discovery writes them; the seeded fault engine (issue 3)
and forced secret resolution (issue 4) read them.

The fault planes match the typed provider ports the engine can perturb. ``fault_rate``
and ``max_latency_s`` are per-plane maps so a test can raise one plane's rate without
perturbing the others; an absent plane defaults to no fault / no delay.
"""

from __future__ import annotations

from typing import Final

SEED_KEY: Final = "seed"
FAULT_RATE_KEY: Final = "fault_rate"
MAX_LATENCY_S_KEY: Final = "max_latency_s"
SECRET_REF_KEY: Final = "secret_ref"
