"""Tests for the lease-window resolver and renewal clamp (`kdive.domain.lease`, ADR-0036)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import LeaseBounds, clamp_extension_hours, resolve_window_hours

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_omitted_window_uses_default() -> None:
    assert resolve_window_hours(None) == Decimal(4)


def test_requested_window_under_max_is_returned_exactly() -> None:
    assert resolve_window_hours("2.5") == Decimal("2.5")


def test_requested_window_over_max_is_clamped() -> None:
    assert resolve_window_hours(48) == Decimal(24)


def test_requested_window_exactly_at_max_is_kept() -> None:
    assert resolve_window_hours(24) == Decimal(24)


@pytest.mark.parametrize("bad", [0, -1, "NaN", "Infinity", "not-a-number", "-0.5"])
def test_non_positive_or_non_finite_window_is_config_error(bad: object) -> None:
    with pytest.raises(CategorizedError) as exc:
        resolve_window_hours(bad)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_custom_default_bound_is_used_for_omitted_window() -> None:
    bounds = LeaseBounds(default_hours=Decimal(8), max_hours=Decimal(24))
    assert resolve_window_hours(None, bounds=bounds) == Decimal(8)


def test_custom_max_bound_tightens_clamp() -> None:
    bounds = LeaseBounds(default_hours=Decimal(4), max_hours=Decimal(2))
    assert resolve_window_hours(10, bounds=bounds) == Decimal(2)


def test_extension_under_cap_bills_full_extend() -> None:
    # Lease expires in 2h; +3h lands at 5h from now, under the 24h cap -> bill 3h.
    expiry = _NOW + timedelta(hours=2)
    result = clamp_extension_hours(expiry, Decimal(3), _NOW)
    assert result.added_hours == Decimal(3)
    assert result.new_expiry == _NOW + timedelta(hours=5)


def test_extension_clamped_to_remaining_window_from_now() -> None:
    # Lease expires in 20h; +10h target = 30h from now, clamped to 24h -> bill 4h.
    expiry = _NOW + timedelta(hours=20)
    result = clamp_extension_hours(expiry, Decimal(10), _NOW)
    assert result.added_hours == Decimal(4)
    assert result.new_expiry == _NOW + timedelta(hours=24)


def test_extension_at_cap_bills_nothing_and_keeps_expiry() -> None:
    # Lease already at the 24h ceiling: any extend yields 0 billable hours, expiry unchanged.
    expiry = _NOW + timedelta(hours=24)
    result = clamp_extension_hours(expiry, Decimal(5), _NOW)
    assert result.added_hours == Decimal(0)
    assert result.new_expiry == expiry


def test_extension_on_expired_lease_bills_from_now_to_cap() -> None:
    # A lapsed lease (expiry in the past) bills from now, not the dead gap, up to the cap.
    expiry = _NOW - timedelta(hours=1)
    result = clamp_extension_hours(expiry, Decimal(100), _NOW)
    assert result.added_hours == Decimal(24)
    assert result.new_expiry == _NOW + timedelta(hours=24)


def test_extension_respects_custom_max_bound() -> None:
    expiry = _NOW + timedelta(hours=1)
    result = clamp_extension_hours(
        expiry,
        Decimal(5),
        _NOW,
        bounds=LeaseBounds(default_hours=Decimal(4), max_hours=Decimal(2)),
    )
    assert result.added_hours == Decimal(1)
    assert result.new_expiry == _NOW + timedelta(hours=2)
