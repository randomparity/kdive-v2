"""Tests for the M0 error taxonomy (`kdive.domain.errors`)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory

# The exact set the spec says M0 may emit (m0-walking-skeleton.md "Error taxonomy"):
# ten reused PoC categories plus six new distributed categories.
M0_PORTED = {
    "configuration_error",
    "missing_dependency",
    "build_failure",
    "boot_timeout",
    "readiness_failure",
    "debug_attach_failure",
    "infrastructure_failure",
    "stale_handle",
    "transport_conflict",
    "not_implemented",
}
M0_DISTRIBUTED = {
    "allocation_denied",
    "lease_expired",
    "provisioning_failure",
    "install_failure",
    "transport_failure",
    "control_failure",
}
M0_ALL = M0_PORTED | M0_DISTRIBUTED


def test_taxonomy_is_exactly_the_m0_set() -> None:
    assert {category.value for category in ErrorCategory} == M0_ALL


@pytest.mark.parametrize("value", sorted(M0_ALL))
def test_each_category_round_trips_to_and_from_its_string(value: str) -> None:
    category = ErrorCategory(value)
    assert category.value == value
    assert str(category) == value
    assert ErrorCategory(category.value) is category


def test_poc_only_test_failure_category_is_not_carried_into_m0() -> None:
    # `test_failure` exists in the PoC enum but M0 has no test plane; the spec's
    # emit-list deliberately omits it, so it must not leak in.
    with pytest.raises(ValueError, match="test_failure"):
        ErrorCategory("test_failure")


def test_categorized_error_is_an_exception_carrying_its_category() -> None:
    error = CategorizedError(
        "domain reported a failure",
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"allocation_id": "abc"},
    )
    assert isinstance(error, Exception)
    assert error.category is ErrorCategory.PROVISIONING_FAILURE
    assert error.details == {"allocation_id": "abc"}
    assert str(error) == "domain reported a failure"


def test_categorized_error_defaults_details_to_empty_dict() -> None:
    error = CategorizedError("no extra context", category=ErrorCategory.STALE_HANDLE)
    assert error.details == {}
