"""Tests for allocation service lease-bound configuration."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.allocation.lease_bounds import configured_lease_bounds


def test_configured_lease_bounds_use_defaults_when_env_absent() -> None:
    bounds = configured_lease_bounds()
    assert bounds.default_hours == Decimal(4)
    assert bounds.max_hours == Decimal(24)


def test_configured_lease_bounds_use_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_DEFAULT", "8")
    monkeypatch.setenv("KDIVE_LEASE_MAX", "12")

    bounds = configured_lease_bounds()

    assert bounds.default_hours == Decimal(8)
    assert bounds.max_hours == Decimal(12)


def test_malformed_default_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_DEFAULT", "nope")

    with pytest.raises(CategorizedError) as exc:
        configured_lease_bounds()

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"env": "KDIVE_LEASE_DEFAULT", "value": "nope"}


def test_malformed_max_env_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LEASE_MAX", "0")

    with pytest.raises(CategorizedError) as exc:
        configured_lease_bounds()

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"env": "KDIVE_LEASE_MAX", "value": "0"}
