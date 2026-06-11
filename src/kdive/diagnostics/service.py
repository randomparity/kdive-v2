"""The aggregating diagnostics service (ADR-0091 §1, §2).

`DiagnosticsService` runs an assembled set of checks — each bounded by the per-check
timeout via :func:`kdive.diagnostics.checks.run_check` — and aggregates them into one
:class:`DiagnosticsReport`. Aggregation keeps the three-state distinction: ``has_failure``
counts only contract violations, and an ``error`` (a check that could not run) never
inflates into a failure.

`doctor` diagnoses a deployment whose **core is up**; it does not replace the health
endpoints (ADR-0090). The worker-vantage checks run as worker jobs, so the service needs
the worker reachable just to *run* them. When the worker is unavailable, those checks
surface as ``error`` results pointing at the health endpoints — **not** a hang, and not a
contract ``fail`` (the tool that explains breakage must not wedge on it).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage, run_check

WORKER_UNAVAILABLE_DETAIL = "worker could not pick up the diagnostic job; check /livez and /readyz"


def worker_unavailable_results(checks: Sequence[Check]) -> list[CheckResult]:
    """Return an ``error`` result per worker-vantage check when the worker is down.

    The result points at the health endpoints (ADR-0090) rather than hanging on the job
    queue — the diagnostic that explains breakage must not wedge on the breakage it exists
    to explain (ADR-0091 §1). It is an ``error``, never a contract ``fail`` (no fix string).
    """
    return [
        CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=WORKER_UNAVAILABLE_DETAIL,
        )
        for check in checks
    ]


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    """One coherent verdict over every run check (ADR-0091 §2)."""

    results: list[CheckResult]

    @property
    def has_failure(self) -> bool:
        """Whether any check reported a contract ``fail`` (a gate must exit nonzero)."""
        return any(r.status is CheckStatus.FAIL for r in self.results)

    @property
    def has_error(self) -> bool:
        """Whether any check could not be run to a verdict (reported distinctly)."""
        return any(r.status is CheckStatus.ERROR for r in self.results)


class DiagnosticsService:
    """Runs the assembled checks and aggregates them into one report."""

    def __init__(
        self,
        *,
        checks: Sequence[Check],
        per_check_timeout: float,
        worker_available: bool = True,
    ) -> None:
        """Build the service.

        Args:
            checks: The assembled checks to run (server- and worker-vantage).
            per_check_timeout: The per-check timeout bound; a check that does not answer
                within it is ``error`` (never a hang).
            worker_available: Whether the worker can pick up worker-vantage jobs. When
                ``False``, worker-vantage checks are not run — they surface as ``error``
                pointing at the health endpoints (ADR-0091 §1).
        """
        self._checks = list(checks)
        self._timeout = per_check_timeout
        self._worker_available = worker_available

    async def run(self) -> DiagnosticsReport:
        """Run every check and return the aggregated report."""
        runnable = [c for c in self._checks if self._can_run(c)]
        skipped = [c for c in self._checks if not self._can_run(c)]
        results = [await run_check(check, timeout=self._timeout) for check in runnable]
        results.extend(worker_unavailable_results(skipped))
        return DiagnosticsReport(results=results)

    def _can_run(self, check: Check) -> bool:
        return self._worker_available or check.vantage is not Vantage.WORKER
