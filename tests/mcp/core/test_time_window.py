"""Shared MCP timestamp-window parsing tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools._time_window import parse_timestamptz_window


def test_parse_timestamptz_window_returns_aware_bounds() -> None:
    window = parse_timestamptz_window(
        ["2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"],
        timestamp_column="ledger.ts",
    )

    assert window == (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 2, 1, tzinfo=UTC),
    )


def test_parse_timestamptz_window_rejects_naive_bounds_with_column_context() -> None:
    with pytest.raises(CategorizedError) as exc:
        parse_timestamptz_window(["2026-01-01T00:00:00", None], timestamp_column="ledger.ts")

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "ledger.ts is timestamptz" in str(exc.value)
