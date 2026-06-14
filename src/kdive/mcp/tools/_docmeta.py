"""Shared documentation metadata for the `@app.tool` wrappers (ADR-0047).

`read_only` / `destructive` / `mutating` build the three MCP `ToolAnnotations`
classes once, so each registration spells its class by name rather than
re-listing hint flags. `DESTRUCTIVE_TOOLS` is the reviewed destructive-
administration set the guard test (`tests/mcp/test_tool_docs.py`) holds the
`destructiveHint` to; its membership is a reviewed judgement (ADR-0047).
"""

from __future__ import annotations

from typing import Literal

from mcp.types import ToolAnnotations

Maturity = Literal["implemented", "partial", "planned"]

DESTRUCTIVE_TOOLS = frozenset(
    {
        "control.power",
        "control.force_crash",
        "systems.teardown",
        "systems.reprovision",
        "ops.force_teardown",
        "ops.force_release",
        "ops.reconcile_systems",
        "resources.drain",
        "resources.deregister",
        "images.delete",
        "images.prune_expired",
        "images.extend",
    }
)


def read_only() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=True)


def destructive() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def mutating() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False)
