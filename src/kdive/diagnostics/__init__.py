"""Server-side diagnostics framework and the read-only `doctor` checks (ADR-0091).

`doctor` diagnoses a deployment whose core is up — it does not replace the health
endpoints (ADR-0090). The public surface is the `Check`/three-state `CheckResult`
abstraction, the three read-only checks, and the aggregating `DiagnosticsService`.
"""

from __future__ import annotations

from kdive.diagnostics.checks import (
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
    run_check,
)
from kdive.diagnostics.service import DiagnosticsReport, DiagnosticsService

__all__ = [
    "Check",
    "CheckResult",
    "CheckStatus",
    "DiagnosticsReport",
    "DiagnosticsService",
    "Vantage",
    "run_check",
]
