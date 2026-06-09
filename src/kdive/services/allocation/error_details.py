"""Safe error-detail propagation for allocation service outcomes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from kdive.domain.errors import CategorizedError


def safe_details(details: Mapping[str, object]) -> dict[str, Any]:
    """Keep JSON-scalar details that are safe to surface in MCP responses."""
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, float):
            if math.isfinite(value):
                safe[key] = value
        elif isinstance(value, (str, bool, int)):
            safe[key] = value
    return safe


def categorized_details(exc: CategorizedError) -> dict[str, Any]:
    """Return the response-safe subset of a categorized error's details."""
    return safe_details(exc.details)
