"""The lease-window resolver used at admission and renewal (ADR-0036 §1, §3).

A grant sets ``lease_expiry = now() + window``; the ``reserved`` estimate is
``rate × window_hours``, so the window is what the project is charged to hold the claim.
:func:`resolve_window_hours` turns the request's optional ``window`` into the concrete,
clamped number of hours admission both bills and stamps:

* an **omitted** window defaults to ``KDIVE_LEASE_DEFAULT`` (4h);
* a **requested** window is validated ``> 0`` (fail-closed, ADR-0007 §2 — a zero or
  negative window would hold a slot for free or mint budget via a negative reserve) and
  clamped to ``KDIVE_LEASE_MAX`` (24h), so one request cannot reserve an unbounded span.

The renewal path (``allocations.renew``, ADR-0036 §3) reuses the same
``KDIVE_LEASE_MAX`` cap against the *remaining* window via :func:`clamp_extension_hours`:
a renew may extend ``lease_expiry`` only up to ``now + KDIVE_LEASE_MAX``, and the project
is charged for the *added* span only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from kdive.domain.cost import parse_window_hours, validate_window
from kdive.domain.errors import CategorizedError, ErrorCategory

_DEFAULT_ENV = "KDIVE_LEASE_DEFAULT"
_MAX_ENV = "KDIVE_LEASE_MAX"
# Operator-configurable bounds (ADR-0036 §1): 4h default when omitted, 24h hard cap.
_DEFAULT_HOURS = Decimal(4)
_MAX_HOURS = Decimal(24)
_SECONDS_PER_HOUR = Decimal(3600)


@dataclass(frozen=True)
class LeaseExtension:
    """The clamped result of a renew: the billable added span and the new expiry.

    ``added_hours`` is the window the project is charged for (``0`` when the lease is
    already at the cap); ``new_expiry`` is the clamped ``lease_expiry`` to persist
    (unchanged from the current expiry when ``added_hours == 0``).
    """

    added_hours: Decimal
    new_expiry: datetime


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


def clamp_extension_hours(
    current_expiry: datetime, requested_extend_hours: Decimal, now: datetime
) -> LeaseExtension:
    """Return the billable added hours and the new expiry, clamped to ``KDIVE_LEASE_MAX``.

    A renew asks to push ``lease_expiry`` out by ``requested_extend_hours`` (already
    validated ``> 0`` by the caller). The new expiry is clamped so the lease never
    extends past ``now + KDIVE_LEASE_MAX`` (ADR-0036 §3): the cap is on the *remaining*
    window measured from ``now``, not on the cumulative lease. The project is charged for
    the added span only, measured from whichever of ``now`` / ``current_expiry`` is
    later — a still-live lease extends contiguously from its current expiry (the agent
    already paid up to it), while a *lapsed* lease bills from ``now`` so the dead past
    gap is never charged. So this returns the billable delta in hours:

    * the base is ``max(now, current_expiry)``;
    * the unclamped target is ``current_expiry + requested_extend_hours``;
    * the ceiling is ``now + KDIVE_LEASE_MAX``;
    * the added hours = ``max(0, min(target, ceiling) − base)`` in hours.

    A lease already at or past the ceiling yields ``0`` (no billable extension); the
    caller treats that as "cannot extend" and leaves the window unchanged.

    Args:
        current_expiry: The allocation's current ``lease_expiry`` (must be non-null —
            a renewable allocation always carries one).
        requested_extend_hours: The requested extension in hours (``> 0``).
        now: The reference instant (the DB ``now()`` the caller read).

    Returns:
        A :class:`LeaseExtension` with the billable added hours (``≥ 0``) and the clamped
        new expiry; ``added_hours == 0`` and ``new_expiry == current_expiry`` when the
        lease is already at the cap.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``KDIVE_LEASE_MAX`` is malformed.
    """
    maximum = _bound_from_env(_MAX_ENV, _MAX_HOURS)
    ceiling = now + timedelta(seconds=int(maximum * _SECONDS_PER_HOUR))
    target = current_expiry + timedelta(seconds=int(requested_extend_hours * _SECONDS_PER_HOUR))
    new_expiry = min(target, ceiling)
    base = max(now, current_expiry)
    if new_expiry <= base:
        return LeaseExtension(added_hours=Decimal(0), new_expiry=current_expiry)
    added_seconds = Decimal((new_expiry - base).total_seconds())
    return LeaseExtension(added_hours=added_seconds / _SECONDS_PER_HOUR, new_expiry=new_expiry)


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
