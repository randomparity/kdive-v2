"""Behavior tests for allocation error detail filtering."""

from __future__ import annotations

import math

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.allocation.error_details import categorized_details, safe_details


def test_safe_details_keeps_json_scalars_and_finite_floats() -> None:
    details = {
        "text": "out of quota",
        "count": 2,
        "enabled": False,
        "ratio": 1.25,
        "none": None,
        "list": ["not", "scalar"],
        "object": {"nested": "not returned"},
        "nan": math.nan,
        "infinity": math.inf,
    }

    assert safe_details(details) == {
        "text": "out of quota",
        "count": 2,
        "enabled": False,
        "ratio": 1.25,
    }


def test_categorized_details_filters_exception_details() -> None:
    exc = CategorizedError(
        "bad allocation request",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"resource_id": "r-1", "debug": object(), "estimate": -math.inf},
    )

    assert categorized_details(exc) == {"resource_id": "r-1"}
