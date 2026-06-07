"""Shared profile-schema validation helpers."""

from __future__ import annotations

from pydantic import field_validator


def reject_coerced_schema_version(value: object) -> object:
    """Reject non-integer schema versions before ``Literal[1]`` coercion."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("schema_version must be an integer")
    return value


schema_version_validator = field_validator("schema_version", mode="before")(
    reject_coerced_schema_version
)
"""Pydantic field validator for profile ``schema_version`` fields."""
