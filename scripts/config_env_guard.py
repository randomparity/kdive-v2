"""Structural guard: no ``KDIVE_*`` env read outside ``kdive.config`` (ADR-0087).

Stdlib-only ``ast`` walk over the source tree so CI runs it without a synced env
(`just config-guard`). It is a rule over the *access form*, not a match on one literal:
it catches ``os.environ.get(...)``, ``os.environ[...]`` and ``os.getenv(...)`` and
resolves a module-level string constant used as the key (the dominant pattern,
``_X_ENV = "KDIVE_..."``). A dynamic, unresolvable key (a generic ``os.environ.get(name)``
helper) is flagged too — it cannot be proven non-``KDIVE_`` and must route through the
registry. A key that resolves to a non-``KDIVE_`` name (``HOME``, ``PATH``) is ignored.

Exit 0 clean, 1 on violations.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "kdive"
_CONFIG_DIR = _SRC / "config"
# Permanent exception: the managed SSH key module is stdlib-only and invoked by the
# builder through the host's python3 (outside the venv), so it cannot import
# kdive.config (ADR-0052). Its KDIVE_SSH_KEY_DIR override is documented in the registry.
_MANAGED_SSH_KEY = _SRC / "prereqs" / "managed_ssh_key.py"
# Shrinking allowlist of files not yet migrated. Must reach empty before the guard gates.
_NOT_YET_MIGRATED: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    variable: str | None  # the resolved KDIVE_* name, or None for a dynamic read


def _str_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Map module-level ``NAME = "string"`` assignments to their string value."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = _str_literal(node.value)
            if value is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = _str_literal(node.value)
            if value is not None:
                consts[node.target.id] = value
    return consts


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _access_key(node: ast.AST) -> ast.AST | None:
    """Return the key-argument node of an ``os.environ``/``os.getenv`` read, else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        func = node.func
        if func.attr == "get" and _is_os_environ(func.value):
            return node.args[0]
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return node.args[0]
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        return node.slice
    return None


def _check_file(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _module_constants(tree)
    out: list[Violation] = []
    for node in ast.walk(tree):
        key = _access_key(node)
        if key is None:
            continue
        name = _str_literal(key)
        if name is None and isinstance(key, ast.Name):
            name = consts.get(key.id)
        line = getattr(node, "lineno", 0)
        if name is None:
            # Dynamic / unresolvable key: cannot prove it is not a KDIVE_* read.
            out.append(Violation(path, line, None))
        elif name.startswith("KDIVE_"):
            out.append(Violation(path, line, name))
    return out


def find_violations(files: list[Path], allowlist: set[Path]) -> list[Violation]:
    out: list[Violation] = []
    for f in files:
        if f in allowlist:
            continue
        out.extend(_check_file(f))
    return out


def _allowlist(files: list[Path]) -> set[Path]:
    allow: set[Path] = {p for p in files if _CONFIG_DIR in p.parents}
    allow.add(_MANAGED_SSH_KEY)
    allow.update(p for p in files if p.name in _NOT_YET_MIGRATED)
    return allow


def main() -> int:
    files = sorted(_SRC.rglob("*.py"))
    violations = find_violations(files, _allowlist(files))
    for v in violations:
        rel = v.file.relative_to(_ROOT)
        what = v.variable if v.variable is not None else "<dynamic env read>"
        print(f"{rel}:{v.line}: {what} read outside kdive.config", file=sys.stderr)
    if violations:
        print(
            f"{len(violations)} stray KDIVE_* env read(s); route through kdive.config",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
