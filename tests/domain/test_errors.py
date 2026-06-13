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
    "authorization_denied",
}
# M1 first-use categories (ADR-0007 §4): the per-project concurrency-cap denial,
# distinct from the over-budget `allocation_denied` so audit/SLO tell count from spend.
# M1.4 (ADR-0069) adds `queue_timeout`: a queued `requested` allocation reaped after the
# max-wait window without ever being placeable — distinct from `lease_expired` (a *granted*
# lease window elapsing), so audit/SLO never conflate a never-placeable backlog request
# with a reclaimed lease.
M1_ADDED = {
    "quota_exceeded",
    "queue_timeout",
}
# Object-lookup categories (#338, ADR-0097): a syntactically valid id that resolves to no
# visible row is `not_found` (distinct from a malformed id, which stays `configuration_error`);
# `conflict` is reserved for a uniqueness/state conflict and is defined-but-unemitted for now.
LOOKUP_ADDED = {
    "not_found",
    "conflict",
}
M0_ALL = M0_PORTED | M0_DISTRIBUTED | M1_ADDED | LOOKUP_ADDED


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


def test_authorization_denied_category_value() -> None:
    assert ErrorCategory.AUTHORIZATION_DENIED.value == "authorization_denied"


def test_not_found_category_value() -> None:
    assert ErrorCategory.NOT_FOUND.value == "not_found"


def test_conflict_category_value() -> None:
    assert ErrorCategory.CONFLICT.value == "conflict"


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
