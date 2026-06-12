"""Resolve operator-configured lease bounds for allocation services."""

from __future__ import annotations

from decimal import Decimal

import kdive.config as config
from kdive.config.core_settings import LEASE_DEFAULT, LEASE_MAX
from kdive.config.registry import Setting
from kdive.domain.cost import parse_window_hours, validate_window
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import DEFAULT_LEASE_BOUNDS, LeaseBounds


def configured_lease_bounds() -> LeaseBounds:
    """Return validated lease bounds from config, falling back to domain defaults."""
    return LeaseBounds(
        default_hours=_bound_from_config(LEASE_DEFAULT, DEFAULT_LEASE_BOUNDS.default_hours),
        max_hours=_bound_from_config(LEASE_MAX, DEFAULT_LEASE_BOUNDS.max_hours),
    )


def _bound_from_config(setting: Setting[str], fallback: Decimal) -> Decimal:
    raw = config.get(setting)
    if raw is None:
        return fallback
    try:
        bound = parse_window_hours(raw)
        validate_window(bound)
    except CategorizedError as exc:
        raise CategorizedError(
            f"{setting.name}={raw!r} must be a finite number of hours > 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"env": setting.name, "value": raw},
        ) from exc
    return bound
