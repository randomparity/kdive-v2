"""Shared parsing for MCP timestamp window filters."""

from __future__ import annotations

from datetime import datetime

from kdive.domain.errors import CategorizedError, ErrorCategory


def parse_timestamptz_window(
    window: object, *, timestamp_column: str
) -> tuple[datetime | None, datetime | None] | None:
    """Parse a timezone-aware ``[start, end]`` window for a timestamptz column."""
    if window is None:
        return None
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise CategorizedError(
            "window must be a [start, end] pair", category=ErrorCategory.CONFIGURATION_ERROR
        )
    start, end = (
        _parse_instant(window[0], timestamp_column),
        _parse_instant(window[1], timestamp_column),
    )
    if start is None and end is None:
        return None
    if start is not None and end is not None and start >= end:
        raise CategorizedError(
            f"window start {start.isoformat()} must precede end {end.isoformat()}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return (start, end)


def _parse_instant(value: object, timestamp_column: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CategorizedError(
            f"window bound {value!r} is not an ISO-8601 string",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CategorizedError(
            f"window bound {value!r} is not a valid ISO-8601 timestamp",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    if parsed.tzinfo is None:
        raise CategorizedError(
            f"window bound {value!r} must be timezone-aware ({timestamp_column} is timestamptz)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed
