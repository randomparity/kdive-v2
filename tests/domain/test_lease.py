"""Tests for the lease-window resolver and renewal clamp (`kdive.domain.lease`, ADR-0036)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import clamp_extension_hours, resolve_window_hours

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


def test_default_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_DEFAULT", "8")
    assert resolve_window_hours(None) == Decimal(8)


def test_max_env_override_tightens_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_MAX", "2")
    assert resolve_window_hours(10) == Decimal(2)


def test_malformed_default_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_DEFAULT", "nope")
    with pytest.raises(CategorizedError) as exc:
        resolve_window_hours(None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_malformed_max_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_MAX", "0")
    with pytest.raises(CategorizedError) as exc:
        resolve_window_hours(5)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


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


def test_extension_respects_max_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_MAX", "2")
    expiry = _NOW + timedelta(hours=1)
    result = clamp_extension_hours(expiry, Decimal(5), _NOW)
    assert result.added_hours == Decimal(1)
    assert result.new_expiry == _NOW + timedelta(hours=2)
