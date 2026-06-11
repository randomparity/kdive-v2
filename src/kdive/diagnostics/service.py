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
from pathlib import Path

import kdive.config as config
from kdive.config.core_settings import SECRETS_ROOT
from kdive.diagnostics.checks import (
    Check,
    CheckResult,
    CheckStatus,
    SecretRefCheck,
    Vantage,
    run_check,
)
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import read_secret_file

WORKER_UNAVAILABLE_DETAIL = "worker could not pick up the diagnostic job; check /livez and /readyz"

_DEFAULT_PER_CHECK_TIMEOUT = 10.0


class _SecretBackendUnreachable(Exception):
    """The secret backend root is absent — a check-cannot-run condition, not a per-ref miss."""


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


def _configured_secret_refs() -> list[tuple[str, bool]]:
    """Collect the ``secret=True`` refs the current environment requires as ``(ref, is_platform)``.

    A setting is checked only when its ``required_when`` predicate holds against the same
    environment snapshot the registry resolves against — the contract :func:`config.validate`
    enforces at startup. This scopes ``secret_ref`` to the refs the deployment actually depends
    on (e.g. the remote-libvirt mTLS refs once ``KDIVE_REMOTE_LIBVIRT_URI`` is set) instead of
    flagging a provider-default ref no active provider needs.

    Every ``KDIVE_*`` secret setting is operator-owned platform config (not tenant data), so
    each is flagged ``is_platform=True`` — naming an unresolved one in the verdict is safe.
    Per-tenant refs (which must never be named) live in the secret registry, not config, and are
    folded in by a later wave; the framework already enforces non-disclosure for them.
    """
    env = config.env_snapshot()
    refs: list[tuple[str, bool]] = []
    for setting in config.all_settings():
        if not setting.secret or not setting.required_when(env):
            continue
        value = config.get(setting)
        if value:
            refs.append((value, True))
    return refs


def _secret_ref_check() -> SecretRefCheck:
    root = Path(config.require(SECRETS_ROOT))
    refs = _configured_secret_refs()

    def _resolve(ref: str) -> None:
        if not root.is_dir():
            raise _SecretBackendUnreachable(str(root))
        try:
            read_secret_file(root, ref)
        except PathSafetyError:
            raise FileNotFoundError(ref) from None

    return SecretRefCheck(
        refs=refs, resolve=_resolve, backend_unreachable=_SecretBackendUnreachable
    )


def default_service_factory(provider: str | None) -> DiagnosticsService:
    """Build the production read-only diagnostics service for ``provider``.

    Assembles the server-vantage ``secret_ref`` check over the configured secret refs,
    resolved against the file-ref backend under ``KDIVE_SECRETS_ROOT``. The worker-vantage
    provider checks (``provider_tls``/``gdbstub_acl``) are assembled with their worker-job
    probe wiring in the egress-probe wave; this factory ships the cheap server-vantage read.
    """
    return DiagnosticsService(
        checks=[_secret_ref_check()], per_check_timeout=_DEFAULT_PER_CHECK_TIMEOUT
    )
