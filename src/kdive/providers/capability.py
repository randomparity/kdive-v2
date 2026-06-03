"""Capability value types and the provider dispatch registry (ADR-0022).

The provider seam's core: providers register capabilities keyed
``(plane, operation, resource_kind)``; the registry dispatches a requested
operation to a provider by capability match, never by name (ADR-0009). The value
types here are frozen, hashable in-memory carriers — not persisted Pydantic
models — so a :class:`Capability` can be a registry key component and an
:class:`OpContract` rejects a malformed ``cleanup`` at construction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.domain.state import ResourceStatus


class Plane(StrEnum):
    """The eight provider planes (ADR-0009). Allocation is core, not a plane."""

    DISCOVERY = "discovery"
    PROVISIONING = "provisioning"
    BUILD = "build"
    INSTALL = "install"
    CONNECT = "connect"
    DEBUG = "debug"
    CONTROL = "control"
    RETRIEVE = "retrieve"


class CleanupGuarantee(StrEnum):
    """An op's cancel/abandon cleanup guarantee (ADR-0009)."""

    CLEAN_ROLLBACK = "clean-rollback"
    BEST_EFFORT = "best-effort"
    ORPHAN_FLAGGED = "orphan-flagged"


@dataclass(frozen=True, slots=True)
class OpContract:
    """Contract flags an operation declares (ADR-0009).

    ``long_running`` routes the op as a job; ``destructive`` drives the
    destructive-op gate; ``cancelable``/``cleanup`` drive cancel and the
    reconciler.
    """

    idempotent: bool
    destructive: bool
    cancelable: bool
    long_running: bool
    cleanup: CleanupGuarantee

    def __post_init__(self) -> None:
        if not isinstance(self.cleanup, CleanupGuarantee):
            raise TypeError(
                f"cleanup must be a CleanupGuarantee, got {type(self.cleanup).__name__}"
            )


@dataclass(frozen=True, slots=True)
class Capability:
    """An advertised operation on a plane for a resource kind, with its contract."""

    plane: Plane
    operation: str
    resource_kind: ResourceKind
    contract: OpContract


@dataclass(frozen=True, slots=True)
class BoundOp:
    """A dispatched operation: the chosen provider's bound method plus its contract.

    Callers read :attr:`contract` for job routing, the destructive-op gate, and the
    reconciler without re-deriving it from the registry.
    """

    provider_id: str
    operation: str
    contract: OpContract
    call: Callable[..., object]


_log = logging.getLogger(__name__)

_HEALTH_RANK: dict[ResourceStatus, int] = {
    ResourceStatus.AVAILABLE: 0,
    ResourceStatus.DEGRADED: 1,
    ResourceStatus.OFFLINE: 2,
}

type _Key = tuple[Plane, str, ResourceKind]


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A registered provider plus the metadata dispatch orders it by."""

    provider: object
    provider_id: str
    health: ResourceStatus
    cost_class: str
    capability: Capability


def _key(capability: Capability) -> _Key:
    return (capability.plane, capability.operation, capability.resource_kind)


class CapabilityRegistry:
    """In-memory registry: register providers, dispatch by capability match.

    Built once at startup and immutable thereafter (ADR-0022) — there is no
    update/replace path. Dispatch never re-queries health; the registration-time
    snapshot is authoritative for the registry's lifetime.
    """

    def __init__(self) -> None:
        self._candidates: dict[_Key, list[_Candidate]] = {}
        self._provider_ids: set[str] = set()

    def register(
        self,
        provider: object,
        capabilities: Sequence[Capability],
        *,
        provider_id: str,
        health: ResourceStatus,
        cost_class: str,
    ) -> None:
        """Register a provider's advertised capabilities atomically.

        Validates everything before mutating registry state; on any failure the
        registry is unchanged and ``provider_id`` stays free.

        Args:
            provider: The provider object; must expose a callable named for each
                capability's ``operation``.
            capabilities: The capabilities this provider advertises.
            provider_id: Stable, non-empty, registry-unique id (the dispatch
                tiebreak).
            health: Registration-time health snapshot.
            cost_class: The provider's cost class (dispatch orders ascending).

        Raises:
            ValueError: Empty/duplicate ``provider_id``, a key advertised twice in
                this call, or a contract that diverges from an existing provider's
                contract for the same key.
            CategorizedError: ``NOT_IMPLEMENTED`` if a capability's ``operation`` is
                not a callable on ``provider``.
        """
        if not provider_id:
            raise ValueError("provider_id must be non-empty")
        if provider_id in self._provider_ids:
            raise ValueError(f"provider_id {provider_id!r} already registered")

        seen: set[_Key] = set()
        for capability in capabilities:
            key = _key(capability)
            if key in seen:
                raise ValueError(f"provider {provider_id!r} advertises {key} twice in one call")
            seen.add(key)
            method = getattr(provider, capability.operation, None)
            if not callable(method):
                raise CategorizedError(
                    f"provider {provider_id!r} advertises {capability.operation!r} "
                    "but has no such method",
                    category=ErrorCategory.NOT_IMPLEMENTED,
                    details={
                        "operation": capability.operation,
                        "provider_id": provider_id,
                    },
                )
            existing = self._candidates.get(key)
            if existing and existing[0].capability.contract != capability.contract:
                raise ValueError(f"contract for {key} diverges from an already-registered provider")

        self._provider_ids.add(provider_id)
        for capability in capabilities:
            self._candidates.setdefault(_key(capability), []).append(
                _Candidate(provider, provider_id, health, cost_class, capability)
            )

    def dispatch(
        self,
        plane: Plane,
        operation: str,
        resource_kind: ResourceKind,
        *,
        pin: str | None = None,
    ) -> BoundOp:
        """Resolve a requested operation to a bound provider op (ADR-0009).

        Selection: an explicit ``pin`` (a ``provider_id``) wins outright; otherwise
        candidates are ordered by health, then ``cost_class`` ascending, then
        ``provider_id`` ascending, and the first is bound. Health orders but never
        filters — an ``offline``-only key still dispatches.

        Args:
            plane: The requested plane.
            operation: The requested operation (a plane method name).
            resource_kind: The resource kind to dispatch for.
            pin: Optional ``provider_id`` to force a specific provider.

        Returns:
            A :class:`BoundOp` carrying the chosen provider's bound method and the
            operation's contract.

        Raises:
            CategorizedError: ``NOT_IMPLEMENTED`` if no provider advertises the key,
                if ``pin`` names a provider that does not advertise it, or if the
                selected provider no longer exposes the method.
        """
        key = (plane, operation, resource_kind)
        details: dict[str, object] = {
            "plane": plane,
            "operation": operation,
            "resource_kind": resource_kind,
            "pin": pin,
        }
        candidates = self._candidates.get(key)
        if not candidates:
            raise CategorizedError(
                f"no provider advertises {key}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        chosen, deciding = self._select(candidates, pin)
        if chosen is None:
            raise CategorizedError(
                f"pin {pin!r} does not advertise {key}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        method = getattr(chosen.provider, operation, None)
        if not callable(method):
            raise CategorizedError(
                f"provider {chosen.provider_id!r} no longer honors {operation!r}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        _log.debug(
            "capability dispatch %s/%s/%s -> %s of %s (by %s)",
            plane,
            operation,
            resource_kind,
            chosen.provider_id,
            [c.provider_id for c in candidates],
            deciding,
        )
        return BoundOp(chosen.provider_id, operation, chosen.capability.contract, method)

    @staticmethod
    def _select(candidates: list[_Candidate], pin: str | None) -> tuple[_Candidate | None, str]:
        """Pick the winning candidate and the step that decided it."""
        if pin is not None:
            for candidate in candidates:
                if candidate.provider_id == pin:
                    return candidate, "pin"
            return None, "pin"
        ordered = sorted(
            candidates,
            key=lambda c: (_HEALTH_RANK[c.health], c.cost_class, c.provider_id),
        )
        winner = ordered[0]
        if len(ordered) == 1:
            return winner, "sole"
        runner_up = ordered[1]
        if winner.health != runner_up.health:
            return winner, "health"
        if winner.cost_class != runner_up.cost_class:
            return winner, "cost_class"
        return winner, "provider_id"
