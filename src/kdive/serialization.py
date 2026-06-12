"""Shared JSON value contracts for database and MCP serialization boundaries."""

from __future__ import annotations

import math
from typing import cast

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


def validate_json_value(value: object, *, path: str) -> None:
    """Validate that ``value`` is a concrete JSON tree."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite JSON number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def ensure_json_value(value: object, *, path: str) -> JsonValue:
    """Return ``value`` typed as JSON after validating its runtime shape."""
    validate_json_value(value, path=path)
    return cast(JsonValue, value)
