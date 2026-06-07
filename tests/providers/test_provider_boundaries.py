"""Provider boundary regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


def test_only_composition_imports_local_libvirt_provider_details() -> None:
    src_root = Path("src/kdive")
    allowed = {
        Path("src/kdive/providers/composition.py"),
        Path("src/kdive/providers/local_libvirt"),
    }
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if any(path == item or item in path.parents for item in allowed):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("kdive.providers.local_libvirt"):
                    offenders.append(f"{path}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("kdive.providers.local_libvirt"):
                        offenders.append(f"{path}:{node.lineno}: import {alias.name}")

    assert offenders == []
