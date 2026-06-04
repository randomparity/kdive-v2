"""The kcu cost model: size-weighted rate, time-scaled cost, fail-closed coeff (ADR-0007 §1-2).

Cost is **size × time** in a dimensionless reference unit (the kcu), so a local-VM Run
and a future cloud Run sum on one axis:

``rate(kcu/hr) = coeff(cost_class) × (W_CPU × vcpus + W_MEM × memory_gb)`` and
``cost(kcu) = rate × hours``. ``W_CPU``/``W_MEM`` are global reference weights pinned by
the ADR (one vcpu-hour ≈ four GB-hours); ``coeff`` is the only per-class number,
resolved from ``cost_class_coefficients`` and **failing closed** (`configuration_error`)
on a missing row — a class with no coefficient is never "free".

All arithmetic uses :class:`~decimal.Decimal` so kcu values stay exact, and every
kcu value the system records or reports passes through :func:`quantize_kcu` — one shared
quantizer so an estimate and the ledger ``reserved``/``reconciled`` deltas that price
the same selector cannot drift by rounding.

:func:`validate_size` / :func:`validate_window` are the fail-closed input guards used by
**both** ``accounting.estimate`` and admission: a ``vcpus < 1``, ``memory_gb < 0``, or
``window ≤ 0`` (or a non-finite / out-of-column-domain) input is rejected as
``configuration_error`` so ``rate`` and ``estimate`` are always ``≥ 0`` — a negative-size
or negative-window request cannot mint budget via a negative ``reserved`` row (ADR-0007
§2). The ``≤ resource-caps`` check is admission-only (it needs a chosen Resource) and
lives with the admission gate, not here.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from kdive.domain.errors import CategorizedError, ErrorCategory

if TYPE_CHECKING:
    from psycopg import AsyncConnection

# Global reference weights (ADR-0007 §1): one vcpu-hour costs 1.0 kcu, one GB-hour 0.25.
W_CPU = Decimal("1.0")
W_MEM = Decimal("0.25")

# Every recorded/reported kcu value quantizes to this scale with banker's rounding, so
# estimate / reserve / reconcile that price the same selector agree to the last place.
KCU_QUANTUM = Decimal("0.0001")

# requested_vcpus / requested_memory_gb persist as Postgres `integer`; the read-side
# estimate rejects anything admission could not store so they share one acceptance domain.
_INT32_MAX = 2**31 - 1


class Selector(BaseModel):
    """The desired size (and cost class) a request or estimate prices.

    ``vcpus`` and ``memory_gb`` are the rate inputs; ``cost_class`` selects the
    coefficient. ``accounting.estimate`` prices a hypothetical selector with no target
    host, so the class is carried here (defaulting to the local baseline) rather than
    read from a Resource — admission instead resolves the class from the chosen Resource.
    """

    model_config = ConfigDict(extra="forbid")

    vcpus: int
    memory_gb: int
    cost_class: str = "local"


def quantize_kcu(value: Decimal) -> Decimal:
    """Quantize a kcu value to :data:`KCU_QUANTUM` with banker's rounding.

    The single rounding point for every kcu the system records or reports, so the
    estimate and the ledger deltas that price one selector cannot diverge by a rounding
    rule (ADR-0007 §2).

    ``validate_window`` deliberately has no upper bound (clamping is admission-only), so a
    read-side estimate can price an arbitrarily large finite window. A product whose
    quantized form would exceed the default decimal precision (``> ~24`` integer digits)
    would raise :class:`~decimal.InvalidOperation`; that is mapped to
    ``configuration_error`` so the value-too-large case fails closed in-category rather
    than escaping as an unhandled exception on a ``viewer``-callable tool.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``value`` is too large to quantize.
    """
    try:
        return value.quantize(KCU_QUANTUM)
    except InvalidOperation:
        raise CategorizedError(
            f"kcu value {value} is too large to price",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"value": str(value)},
        ) from None


def rate(coeff: Decimal, *, vcpus: int, memory_gb: int) -> Decimal:
    """Return the exact (unquantized) kcu/hr rate for ``coeff`` and a size.

    ``rate = coeff × (W_CPU × vcpus + W_MEM × memory_gb)``. Exact so callers quantize
    once at the reporting/recording boundary; never rounds here.
    """
    return coeff * (W_CPU * vcpus + W_MEM * memory_gb)


def cost(rate_kcu_per_hr: Decimal, hours: Decimal) -> Decimal:
    """Return the exact (unquantized) kcu cost of ``rate_kcu_per_hr`` over ``hours``."""
    return rate_kcu_per_hr * hours


def parse_window_hours(window: object) -> Decimal:
    """Parse a request ``window`` into a positive number of hours (Decimal).

    The wire ``window`` is a number of hours; it is carried as :class:`~decimal.Decimal`
    so the estimate and the admission reservation that price the same window agree
    exactly. A value that is not a finite number (``None``, a non-numeric string, ``NaN``,
    ``Infinity``) is a ``configuration_error`` — the same fail-closed discipline as
    :func:`validate_window`, applied at the wire boundary.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``window`` is not a finite number.
    """
    try:
        parsed = Decimal(str(window))
    except (InvalidOperation, DecimalException, ValueError, TypeError):
        raise CategorizedError(
            f"window {window!r} is not a number",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    return parsed


def validate_size(selector: Selector) -> None:
    """Reject a selector that would price a negative or unstorable rate (fail closed).

    Rejects ``vcpus < 1``, ``memory_gb < 0``, and any size outside the persisted
    ``integer`` column domain, so ``rate ≥ 0`` and the read-side estimate never accepts a
    size admission could not store (ADR-0007 §2).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for any rejected size.
    """
    if selector.vcpus < 1:
        raise _size_error("vcpus", selector.vcpus, "must be ≥ 1")
    if selector.memory_gb < 0:
        raise _size_error("memory_gb", selector.memory_gb, "must be ≥ 0")
    if selector.vcpus > _INT32_MAX:
        raise _size_error("vcpus", selector.vcpus, f"must be ≤ {_INT32_MAX}")
    if selector.memory_gb > _INT32_MAX:
        raise _size_error("memory_gb", selector.memory_gb, f"must be ≤ {_INT32_MAX}")


def validate_window(window: Decimal) -> None:
    """Reject a non-positive or non-finite window (fail closed).

    Guards ``window ≤ 0`` **and** ``NaN``/``Infinity`` — ``NaN ≤ 0`` is ``False``, so a
    naive sign check would let a ``NaN`` window through and yield a ``NaN`` estimate that
    the budget compare mishandles (ADR-0007 §2).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``window`` is not a finite ``> 0``.
    """
    if not window.is_finite():
        raise _window_error(window, "must be a finite number")
    if window <= 0:
        raise _window_error(window, "must be > 0")


async def resolve_coeff(conn: AsyncConnection, cost_class: str) -> Decimal:
    """Resolve the coefficient for ``cost_class`` from ``cost_class_coefficients``.

    Fails closed: a class with no row is a ``configuration_error``, never "free"
    (ADR-0007 §1). Reads the persisted class, never request data.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``cost_class`` has no coefficient row.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (cost_class,)
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"cost_class {cost_class!r} has no coefficient row",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"cost_class": cost_class},
        )
    return Decimal(row[0])


def _size_error(field: str, value: int, requirement: str) -> CategorizedError:
    return CategorizedError(
        f"selector {field}={value} {requirement}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": field, "value": str(value)},
    )


def _window_error(window: Decimal, requirement: str) -> CategorizedError:
    return CategorizedError(
        f"window={window} {requirement}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"window": str(window)},
    )
