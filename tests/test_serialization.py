"""Public JSON serialization contract tests."""

from __future__ import annotations

import math
import re

import pytest

from kdive.serialization import ensure_json_value, validate_json_value


def test_ensure_json_value_accepts_nested_json_tree() -> None:
    value = {
        "status": "ok",
        "attempts": 2,
        "ratio": 0.5,
        "enabled": True,
        "items": [{"name": "kernel", "refs": ["a", "b"]}, None],
    }

    validated = ensure_json_value(value, path="payload")

    assert validated == value


@pytest.mark.parametrize("number", [math.inf, -math.inf, math.nan])
def test_validate_json_value_rejects_non_finite_numbers_with_path(number: float) -> None:
    with pytest.raises(ValueError, match=re.escape("payload.score must be finite JSON number")):
        validate_json_value({"score": number}, path="payload")


def test_validate_json_value_rejects_non_string_dict_keys_with_path() -> None:
    with pytest.raises(ValueError, match=re.escape("payload.items[0] object keys must be strings")):
        validate_json_value({"items": [{1: "rootfs"}]}, path="payload")


def test_validate_json_value_rejects_nested_invalid_values_with_path() -> None:
    invalid = {"items": [{"metadata": {"owner": object()}}]}

    with pytest.raises(
        ValueError,
        match=re.escape("payload.items[0].metadata.owner contains non-JSON value object"),
    ):
        validate_json_value(invalid, path="payload")
