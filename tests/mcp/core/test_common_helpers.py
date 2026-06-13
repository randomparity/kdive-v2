"""Unit tests for the shared MCP tool-boundary helpers (`kdive.mcp.tools._common`)."""

from __future__ import annotations

from kdive.mcp.tools._common import config_error, not_found


def test_not_found_builds_a_not_found_failure_envelope() -> None:
    resp = not_found("abc")
    assert resp.status == "error"
    assert resp.error_category == "not_found"
    assert resp.object_id == "abc"
    assert resp.data == {}


def test_not_found_carries_optional_data() -> None:
    resp = not_found("abc", data={"hint": "gone"})
    assert resp.error_category == "not_found"
    assert resp.data == {"hint": "gone"}


def test_config_error_stays_configuration_error() -> None:
    # The two helpers must remain distinct: a malformed id stays configuration_error.
    resp = config_error("nope")
    assert resp.error_category == "configuration_error"
