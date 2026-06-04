"""The lease-window resolver used at admission (ADR-0036 §1).

A grant sets ``lease_expiry = now() + window``; the ``reserved`` estimate is
``rate × window_hours``, so the window is what the project is charged to hold the claim.
:func:`resolve_window_hours` turns the request's optional ``window`` into the concrete,
clamped number of hours admission both bills and stamps:

* an **omitted** window defaults to ``KDIVE_LEASE_DEFAULT`` (4h);
* a **requested** window is validated ``> 0`` (fail-closed, ADR-0007 §2 — a zero or
  negative window would hold a slot for free or mint budget via a negative reserve) and
  clamped to ``KDIVE_LEASE_MAX`` (24h), so one request cannot reserve an unbounded span.

The renewal path (``allocations.renew``, ADR-0036 §3) is a separate issue and reuses
this clamp against the *remaining* window; only the at-grant resolution lives here.
"""

from __future__ import annotations

import os
from decimal import Decimal

from kdive.domain.cost import parse_window_hours, validate_window
from kdive.domain.errors import CategorizedError, ErrorCategory

_DEFAULT_ENV = "KDIVE_LEASE_DEFAULT"
_MAX_ENV = "KDIVE_LEASE_MAX"
# Operator-configurable bounds (ADR-0036 §1): 4h default when omitted, 24h hard cap.
_DEFAULT_HOURS = Decimal(4)
_MAX_HOURS = Decimal(24)


def resolve_window_hours(window: object | None) -> Decimal:
    """Resolve and clamp the lease window (in hours) for an admission grant.

    An omitted (``None``) window uses ``KDIVE_LEASE_DEFAULT``; a supplied window is
    parsed, validated ``> 0`` and finite (``configuration_error`` otherwise), then
    clamped to ``KDIVE_LEASE_MAX``. The returned value is the exact hours both the
    reserved estimate and ``lease_expiry`` use.

    Args:
        window: The request's requested window in hours (a number or decimal string),
            or ``None`` to take the default.

    Returns:
        The clamped, validated window in hours as an exact :class:`~decimal.Decimal`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if a supplied window is not a finite
            ``> 0`` number, or if a configured bound env var is malformed.
    """
    if window is None:
        return _bound_from_env(_DEFAULT_ENV, _DEFAULT_HOURS)
    requested = parse_window_hours(window)
    validate_window(requested)
    maximum = _bound_from_env(_MAX_ENV, _MAX_HOURS)
    return min(requested, maximum)


def _bound_from_env(name: str, fallback: Decimal) -> Decimal:
    """Read a positive lease-bound (hours) from ``name``; fall back when unset.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the env var is set but not a
            finite ``> 0`` number — an operator misconfiguration fails closed rather
            than silently reverting to the built-in default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    bound = parse_window_hours(raw)
    try:
        validate_window(bound)
    except CategorizedError as exc:
        raise CategorizedError(
            f"{name}={raw!r} must be a finite number of hours > 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"env": name, "value": raw},
        ) from exc
    return bound
