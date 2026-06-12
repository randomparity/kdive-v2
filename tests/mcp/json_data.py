"""Typed accessors for JSON-shaped ``ToolResponse.data`` in tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from kdive.mcp.responses import JsonValue, ToolResponse


def data_str(resp: ToolResponse, key: str) -> str:
    value = resp.data[key]
    assert isinstance(value, str)
    return value


def data_mapping(resp: ToolResponse, key: str) -> Mapping[str, JsonValue]:
    value = resp.data[key]
    assert isinstance(value, Mapping)
    return cast(Mapping[str, JsonValue], value)


def data_sequence(resp: ToolResponse, key: str) -> Sequence[JsonValue]:
    value = resp.data[key]
    assert isinstance(value, Sequence)
    assert not isinstance(value, str)
    return cast(Sequence[JsonValue], value)


def json_mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, JsonValue], value)


def json_sequence(value: JsonValue) -> Sequence[JsonValue]:
    assert isinstance(value, Sequence)
    assert not isinstance(value, str)
    return cast(Sequence[JsonValue], value)
