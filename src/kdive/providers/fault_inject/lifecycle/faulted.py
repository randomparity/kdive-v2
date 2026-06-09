"""Faulting wrappers that thread the seeded engine into the fault-inject ports (ADR-0074).

The happy-path mock ports stay synthetic and untouched; these thin
decorators consult a :class:`~kdive.providers.fault_inject.faulting.engine.FaultEngine` before
delegating, so a seeded fault actually perturbs the spine op:

- a drawn ``fail`` raises ``CategorizedError(decision.category)`` **iff** ``decision.fail``
  (the catalog category, never ``lease_expired``);
- a drawn ``latency`` blocks the op for the engine-computed delay via an injected ``sleep_s``
  seam (default :func:`time.sleep`) — the ports are synchronous and the worker offloads them
  via ``asyncio.to_thread``, so a blocking sleep there matches a real slow provider without
  stalling the event loop;
- ``attempt`` is a caller-supplied **durable** input (``attempt_for``, default first attempt),
  never a wrapper-held counter (ADR-0072's determinism leg).

A wrapper is assembled only when fault config is present (``build_faultinject_runtime`` with a
non-None ``engine``); the happy-path composition stays unchanged when no fault is configured.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from kdive.domain.errors import CategorizedError
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.faulting.engine import FaultDecision, FaultEngine, FaultPlane
from kdive.providers.fault_inject.lifecycle.provider import FaultInjectInstall, FaultInjectProvision
from kdive.providers.ports import InstallRequest

_FIRST_ATTEMPT: Callable[[UUID], int] = lambda _system_id: 1  # noqa: E731 - a tiny default port
_SyncSleep = Callable[[float], None]
_AttemptFor = Callable[[UUID], int]


def _apply(decision: FaultDecision, sleep_s: _SyncSleep) -> None:
    """Realize a decision: sleep the drawn latency, then raise iff the draw failed.

    Latency is applied before a failure raise so a slow-then-failing op spends its delay
    (matching a provider that hangs then errors). Raises ``CategorizedError`` only when
    ``decision.fail`` is true, in which case ``decision.category`` is guaranteed non-None.
    """
    if decision.latency_s > 0.0:
        sleep_s(decision.latency_s)
    if decision.fail:
        assert decision.category is not None  # noqa: S101 - engine invariant: fail => category
        raise CategorizedError(
            f"fault-inject drew a {decision.category.value} failure",
            category=decision.category,
        )


class FaultedProvision:
    """A :class:`FaultInjectProvision` that draws provision-plane faults before delegating."""

    def __init__(
        self,
        inner: FaultInjectProvision,
        engine: FaultEngine,
        *,
        attempt_for: _AttemptFor = _FIRST_ATTEMPT,
        sleep_s: _SyncSleep = time.sleep,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._attempt_for = attempt_for
        self._sleep_s = sleep_s

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        self._draw(system_id, FaultPlane.PROVISION)
        return self._inner.provision(system_id, profile)

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        self._draw(system_id, FaultPlane.PROVISION)
        return self._inner.reprovision(system_id, profile)

    def teardown(self, domain_name: str) -> None:
        # Teardown is compensation, not a perturbed op — it must always reap, so no draw.
        self._inner.teardown(domain_name)

    def _draw(self, system_id: UUID, plane: FaultPlane) -> None:
        decision = self._engine.decide(
            system_id=system_id, plane=plane, attempt=self._attempt_for(system_id)
        )
        _apply(decision, self._sleep_s)


class FaultedInstall:
    """A :class:`FaultInjectInstall` that draws install/boot-plane faults before delegating."""

    def __init__(
        self,
        inner: FaultInjectInstall,
        engine: FaultEngine,
        *,
        attempt_for: _AttemptFor = _FIRST_ATTEMPT,
        sleep_s: _SyncSleep = time.sleep,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._attempt_for = attempt_for
        self._sleep_s = sleep_s

    def install(self, request: InstallRequest) -> None:
        self._draw(request.system_id, FaultPlane.INSTALL)
        self._inner.install(request)

    def boot(self, system_id: UUID) -> None:
        self._draw(system_id, FaultPlane.BOOT)
        self._inner.boot(system_id)

    def _draw(self, system_id: UUID, plane: FaultPlane) -> None:
        decision = self._engine.decide(
            system_id=system_id, plane=plane, attempt=self._attempt_for(system_id)
        )
        _apply(decision, self._sleep_s)
