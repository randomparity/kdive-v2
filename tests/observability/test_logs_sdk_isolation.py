"""The pre-stable `_logs` SDK is confined to kdive/observability (ADR-0090 §7).

The OTel logs signal still lives under the `_logs` underscore namespace; isolating
every import of it behind the facade means an upstream API shift is a single-package
change and the stdout floor is never hostage to a pre-stable surface.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "kdive"
_FACADE_PKG = "kdive/observability/"
_FORBIDDEN_PREFIXES = ("opentelemetry.sdk._logs", "opentelemetry._logs")


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_only_observability_touches_logs_sdk() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC.parent).as_posix()
        if rel.startswith(_FACADE_PKG):
            continue
        for imported in _imports(path):
            if any(imported.startswith(p) for p in _FORBIDDEN_PREFIXES):
                offenders.append(f"{rel}: {imported}")
    assert not offenders, f"`_logs` SDK imported outside the facade: {offenders}"


def test_facade_does_import_logs_sdk() -> None:
    # Guards the test above against vacuity: the facade must actually own the import.
    facade_imports: list[str] = []
    for path in (_SRC / "observability").rglob("*.py"):
        facade_imports.extend(_imports(path))
    assert any(imp.startswith(p) for imp in facade_imports for p in _FORBIDDEN_PREFIXES), (
        "the facade should own the `_logs` SDK import"
    )
