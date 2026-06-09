"""The seeded decision-keyed fault engine (ADR-0072, M1.5 issue 3).

Every fault decision is a **pure function of stable inputs**:

    fault_for(seed, system_id, plane, attempt, facet) -> draw in [0, 1)

— not a step of a shared mutable PRNG stream (order-dependent under concurrent workers).
Three details are load-bearing and must not be skipped (ADR-0072):

- **The hash is process-independent.** Python's builtin ``hash()`` salts ``str``/``bytes``
  per process (``PYTHONHASHSEED``), so it would yield different draws across the concurrent
  workers M1.5 runs. The draw is computed with :func:`hashlib.blake2b` over a canonical byte
  encoding of the key, **never** builtin ``hash()`` — a determinism guard test asserts the
  draw is identical across two subprocesses launched with different ``PYTHONHASHSEED``.
- **``attempt`` derives from durable state**, never a process-local counter. The engine takes
  ``attempt`` as a parameter (the caller supplies the Run boot ordinal / attach ordinal /
  persisted retry count); it never reads or increments a counter of its own.
- **Each ``facet`` is its own keyed draw** (``fail`` / ``category`` / ``latency``), so the
  three decisions a plane makes stay independent and reproducible.

The engine reads ``seed``, the per-plane ``fault_rate`` map, and the per-plane
``max_latency_s`` bound from the resource ``capabilities`` jsonb (issue-2 keys) — never from
wall-clock or ``os.urandom``. This module imports no nondeterministic source; a guard test
enforces that.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    MAX_LATENCY_S_KEY,
    SEED_KEY,
)

_KEY_SEPARATOR: Final = b"\x00"
_DIGEST_BYTES: Final = 8
# Keep the top 53 bits (an IEEE-754 double's mantissa) and divide by 2**53: every value of
# the numerator is exactly representable and strictly below the denominator, so the draw is
# provably in [0, 1). Dividing the full 64-bit integer by 2**64 instead rounds the top values
# up to exactly 1.0, which would break the [0, 1) contract (a fault_rate=1.0 plane could then
# evaluate ``1.0 < 1.0`` False and silently not fail).
_MANTISSA_BITS: Final = 53
_DROPPED_BITS: Final = 8 * _DIGEST_BYTES - _MANTISSA_BITS
_UNIT_SCALE: Final = float(1 << _MANTISSA_BITS)


class FaultPlane(StrEnum):
    """The six perturbable provider planes the engine keys faults on.

    The values are stable wire strings used both as hash-key material and as the keys of
    the per-plane ``fault_rate`` / ``max_latency_s`` capability maps.
    """

    PROVISION = "provision"
    INSTALL = "install"
    BOOT = "boot"
    CONNECT = "connect"
    CONTROL = "control"
    RETRIEVE = "retrieve"


class FaultFacet(StrEnum):
    """The three independent decisions a plane draws for one ``(system_id, attempt)``."""

    FAIL = "fail"
    CATEGORY = "category"
    LATENCY = "latency"


# Per-plane fault catalog → existing ``ErrorCategory`` only (the spec forbids new strings).
# A plane with two categories lets the ``category`` draw bucket across both.
_FAULT_CATALOG: Final[Mapping[FaultPlane, tuple[ErrorCategory, ...]]] = {
    FaultPlane.PROVISION: (ErrorCategory.PROVISIONING_FAILURE,),
    FaultPlane.INSTALL: (ErrorCategory.INSTALL_FAILURE, ErrorCategory.BOOT_TIMEOUT),
    FaultPlane.BOOT: (ErrorCategory.READINESS_FAILURE, ErrorCategory.BOOT_TIMEOUT),
    FaultPlane.CONNECT: (ErrorCategory.TRANSPORT_FAILURE,),
    FaultPlane.CONTROL: (ErrorCategory.CONTROL_FAILURE,),
    FaultPlane.RETRIEVE: (ErrorCategory.INFRASTRUCTURE_FAILURE,),
}


def fault_for(
    *,
    seed: int,
    system_id: UUID,
    plane: FaultPlane,
    attempt: int,
    facet: FaultFacet,
) -> float:
    """Return the stable draw in ``[0, 1)`` for one fault-decision key.

    The key's five fields are each encoded to a decimal/text byte form — none can contain a
    NUL byte — then joined with a single NUL separator, so the join is an injective encoding
    of the 5-tuple (distinct keys never collide on the joined bytes). The top 53 bits of the
    :func:`hashlib.blake2b` digest divided by ``2**53`` land provably in ``[0, 1)``.

    Args:
        seed: The resource-configured seed (part of every draw key).
        system_id: The System the decision is for.
        plane: The perturbable plane.
        attempt: The durable attempt ordinal (1-based) — never a process-local counter.
        facet: Which of the three independent draws to compute.

    Returns:
        A deterministic, process-independent draw in ``[0, 1)``.

    Raises:
        ValueError: If ``attempt < 1`` (durable ordinals are 1-based; a non-positive attempt
            is a caller bug, not a silently-drawn case).
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1 (a 1-based durable ordinal), got {attempt}")
    fields = (
        str(seed).encode(),
        str(system_id).encode(),
        plane.value.encode(),
        str(attempt).encode(),
        facet.value.encode(),
    )
    digest = hashlib.blake2b(_KEY_SEPARATOR.join(fields), digest_size=_DIGEST_BYTES).digest()
    return (int.from_bytes(digest, "big") >> _DROPPED_BITS) / _UNIT_SCALE


@dataclass(frozen=True, slots=True)
class FaultDecision:
    """The engine's decision for one ``(system_id, plane, attempt)``.

    Attributes:
        fail: Whether the plane draws a failure (``fault_for(fail) < fault_rate[plane]``).
        category: The bucketed :class:`ErrorCategory` when ``fail``, else ``None``.
        latency_s: The scaled delay (``fault_for(latency) * max_latency_s[plane]``); ``>= 0``.
    """

    fail: bool
    category: ErrorCategory | None
    latency_s: float


@dataclass(frozen=True, slots=True)
class FaultEngine:
    """A seeded fault engine over a per-plane ``fault_rate`` map and ``max_latency_s`` bound.

    An absent plane defaults to no fault (rate 0) and no delay (bound 0).
    """

    seed: int
    fault_rate: Mapping[str, float]
    max_latency_s: Mapping[str, float]

    def decide(self, *, system_id: UUID, plane: FaultPlane, attempt: int) -> FaultDecision:
        """Return the :class:`FaultDecision` for one op, threading ``self.seed`` into every draw.

        Args:
            system_id: The System the op targets.
            plane: The perturbable plane.
            attempt: The durable attempt ordinal (1-based).
        """
        fail = self._draw(system_id, plane, attempt, FaultFacet.FAIL) < self.fault_rate.get(
            plane, 0.0
        )
        category = self._category(system_id, plane, attempt) if fail else None
        latency_s = self._draw(
            system_id, plane, attempt, FaultFacet.LATENCY
        ) * self.max_latency_s.get(plane, 0.0)
        return FaultDecision(fail=fail, category=category, latency_s=latency_s)

    def _draw(self, system_id: UUID, plane: FaultPlane, attempt: int, facet: FaultFacet) -> float:
        return fault_for(
            seed=self.seed, system_id=system_id, plane=plane, attempt=attempt, facet=facet
        )

    def _category(self, system_id: UUID, plane: FaultPlane, attempt: int) -> ErrorCategory:
        catalog = _FAULT_CATALOG[plane]
        draw = self._draw(system_id, plane, attempt, FaultFacet.CATEGORY)
        index = min(int(draw * len(catalog)), len(catalog) - 1)
        return catalog[index]

    @classmethod
    def from_capabilities(cls, capabilities: Mapping[str, object]) -> FaultEngine:
        """Build an engine from the fault-inject resource's ``capabilities`` jsonb.

        Args:
            capabilities: The resource capabilities; an absent ``seed`` defaults to 0 and
                absent ``fault_rate`` / ``max_latency_s`` default to empty maps.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if a ``fault_rate`` value is outside
                ``[0, 1]`` or a ``max_latency_s`` value is negative.
        """
        fault_rate = _float_map(capabilities.get(FAULT_RATE_KEY))
        max_latency_s = _float_map(capabilities.get(MAX_LATENCY_S_KEY))
        for plane, rate in fault_rate.items():
            if not 0.0 <= rate <= 1.0:
                raise CategorizedError(
                    f"fault_rate[{plane!r}]={rate} is outside [0, 1]",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                )
        for plane, bound in max_latency_s.items():
            if bound < 0.0:
                raise CategorizedError(
                    f"max_latency_s[{plane!r}]={bound} is negative",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                )
        return cls(
            seed=_coerce_seed(capabilities.get(SEED_KEY, 0)),
            fault_rate=fault_rate,
            max_latency_s=max_latency_s,
        )


def _coerce_seed(raw: object) -> int:
    """Coerce a capabilities ``seed`` to ``int``, rejecting a non-integer value."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CategorizedError(
            f"seed must be an integer, got {type(raw).__name__}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return raw


def _coerce_rate(plane: object, raw: object) -> float:
    """Coerce a per-plane capability value to ``float``, rejecting a non-numeric value."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CategorizedError(
            f"per-plane value for {plane!r} must be numeric, got {type(raw).__name__}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return float(raw)


def _float_map(raw: object) -> dict[str, float]:
    """Coerce a capabilities sub-map to ``{str: float}`` (an absent/None value is empty).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``raw`` is not a map or a value is
            non-numeric.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CategorizedError(
            f"expected a per-plane map, got {type(raw).__name__}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return {str(plane): _coerce_rate(plane, value) for plane, value in raw.items()}
