"""The shared backend-health probe (ADR-0090 §5).

A :class:`HealthProbe` composes a list of named :class:`BackendCheck` async checks and
answers ``/readyz``. The dependency set is **injected, not hardcoded**: the server
composes Postgres + MinIO + OIDC; the worker/reconciler (issue #267) compose Postgres +
MinIO with no IdP coupling — both build a :class:`HealthProbe` over the same primitive.

Two contractual behaviors:

- **Caching asymmetry.** A healthy result is cached for a short TTL (smoothing probe
  load and brief blips); a failing result is reflected *immediately* and never cached,
  so caching never opens a "ready-while-down" window.
- **Per-check timeout.** Each check is bounded; a hung backend reads as *down*, not as a
  stalled probe.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

#: Default per-check timeout (seconds): a hung backend reads as down within this bound.
DEFAULT_CHECK_TIMEOUT = 2.0
#: Default healthy-result cache TTL (seconds): smooths probe load under K8s cadence.
DEFAULT_HEALTHY_TTL = 5.0


@dataclass(frozen=True, slots=True)
class BackendCheck:
    """One named readiness check over a single backend dependency.

    Args:
        name: Stable identifier for the dependency (e.g. ``"postgres"``); surfaced in the
            ``/readyz`` body and is the only label that reaches a metric/span.
        probe: A zero-argument coroutine that returns on success and raises on failure.
            It must not block the event loop; offload synchronous client calls.
    """

    name: str
    probe: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReadyResult:
    """The outcome of a probe pass: overall readiness plus per-check booleans."""

    ready: bool
    checks: dict[str, bool] = field(default_factory=dict)


class HealthProbe:
    """Compose backend checks into a cached, timeout-bounded readiness probe.

    Args:
        checks: The dependency set this process gates readiness on. An empty set is
            always ready (a process with no backends).
        check_timeout: Per-check timeout in seconds; a check exceeding it reads as down.
        healthy_ttl: Seconds an all-healthy result is cached. ``0`` disables caching.
    """

    def __init__(
        self,
        *,
        checks: list[BackendCheck],
        check_timeout: float = DEFAULT_CHECK_TIMEOUT,
        healthy_ttl: float = DEFAULT_HEALTHY_TTL,
    ) -> None:
        self._checks = list(checks)
        self._check_timeout = check_timeout
        self._healthy_ttl = healthy_ttl
        self._cached: ReadyResult | None = None
        self._cached_at = 0.0

    async def check(self) -> ReadyResult:
        """Return readiness, honoring the healthy-cached / failure-immediate asymmetry.

        A still-fresh healthy cache entry is returned without re-probing the backends;
        a failing result is never cached, so the next call re-probes and recovery is
        reflected at once.
        """
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        result = await self._run_checks()
        if result.ready and self._healthy_ttl > 0.0:
            self._cached = result
            self._cached_at = time.monotonic()
        else:
            self._cached = None
        return result

    def _fresh_cached(self) -> ReadyResult | None:
        if self._cached is None:
            return None
        if time.monotonic() - self._cached_at < self._healthy_ttl:
            return self._cached
        self._cached = None
        return None

    async def _run_checks(self) -> ReadyResult:
        results = await asyncio.gather(*(self._run_one(c) for c in self._checks))
        per_check = dict(results)
        return ReadyResult(ready=all(per_check.values()), checks=per_check)

    async def _run_one(self, check: BackendCheck) -> tuple[str, bool]:
        try:
            await asyncio.wait_for(check.probe(), timeout=self._check_timeout)
        except Exception:  # noqa: BLE001 - any failure (incl. timeout) reads as down
            return check.name, False
        return check.name, True
