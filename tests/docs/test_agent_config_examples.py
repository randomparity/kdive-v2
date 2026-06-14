"""The shipped agent config examples must be valid JSON with an mcpServers.kdive entry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "docs" / "guide" / "agents"


@pytest.mark.parametrize("name", ["mcp.json", "claude_desktop_config.json"])
def test_example_is_valid_json_with_kdive_server(name: str) -> None:
    data = json.loads((AGENTS / name).read_text())
    assert "kdive" in data["mcpServers"]
