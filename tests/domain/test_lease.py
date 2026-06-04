"""Tests for the at-grant lease-window resolver (`kdive.domain.lease`, ADR-0036 §1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lease import resolve_window_hours


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
