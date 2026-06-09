"""Provider-runtime boundary tests for MCP assembly (ADR-0071)."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MCP_ROOT = _REPO_ROOT / "src" / "kdive" / "mcp"


def test_mcp_modules_do_not_bind_local_runtime_directly() -> None:
    offenders: list[str] = []
    for path in sorted(_MCP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "kdive.providers.composition":
                for alias in node.names:
                    if alias.name == "build_local_runtime":
                        offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_local_runtime"
            ):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert not offenders
