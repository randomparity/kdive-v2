"""Shared profile schema helper tests."""

from __future__ import annotations

import pytest

from kdive.profiles._schema import reject_coerced_schema_version


def test_reject_coerced_schema_version_accepts_plain_int() -> None:
    assert reject_coerced_schema_version(1) == 1


@pytest.mark.parametrize("value", ["1", 1.0, True, None])
def test_reject_coerced_schema_version_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match="schema_version must be an integer"):
        reject_coerced_schema_version(value)
