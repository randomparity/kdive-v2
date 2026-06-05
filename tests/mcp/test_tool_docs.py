# tests/mcp/test_tool_docs.py
"""The ADR-0047 documentation guard, over the live FastMCP registry.

Builds the app with a null pool + a local-keypair verifier (the service-test
path; needs no DB and no OIDC env), then asserts every tool is fully
documented, the destructive hint matches the reviewed set, and every
`implemented` tool's wrapper callee is referenced by a non-live test.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.tools import _docmeta
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"
# Common callees every wrapper names; never a tool-unique anchor.
_SHARED_CALLEES = frozenset({"current_context"})


def _build_tools() -> list[FunctionTool]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier)
    # list_tools() is typed as Sequence[mcp.types.Tool] but the fastmcp runtime
    # returns list[FunctionTool] — cast to the concrete type so the rest of the
    # module can access .fn / .meta / .annotations without type errors.
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def _callees(fn: Callable[..., Any]) -> set[str]:
    """The set of called symbol names in a wrapper body (Name + Attribute calls).

    The wrappers are closures nested inside ``register()``, so ``inspect.getsource``
    returns them at their nesting indent (decorator included); ``textwrap.dedent`` is
    required or ``ast.parse`` raises ``IndentationError``.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _unique_anchor(tool: FunctionTool, freq: Counter[str]) -> str:
    """The single callee unique to this wrapper; hard-fail on zero or >1."""
    candidates = {c for c in _callees(tool.fn) if c not in _SHARED_CALLEES and freq[c] == 1}
    assert len(candidates) == 1, (
        f"{tool.name}: expected exactly one tool-unique callee, got {sorted(candidates)}"
    )
    return next(iter(candidates))


def _test_sources() -> str:
    """All non-live test source concatenated (live_vm/live_stack files excluded)."""
    blobs: list[str] = []
    for path in _TESTS_DIR.rglob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if "live_vm" in text or "live_stack" in text:
            continue
        blobs.append(text)
    return "\n".join(blobs)


TOOLS = _build_tools()
FREQ: Counter[str] = Counter()
for _t in TOOLS:
    FREQ.update(c for c in _callees(_t.fn) if c not in _SHARED_CALLEES)


def test_every_tool_has_a_description() -> None:
    missing = [t.name for t in TOOLS if not (t.description or "").strip()]
    assert not missing, f"tools missing a description: {missing}"


def test_every_parameter_has_a_description() -> None:
    offenders: list[str] = []
    for t in TOOLS:
        props = (t.parameters or {}).get("properties", {})
        for param, schema in props.items():
            if not (schema.get("description") or "").strip():
                offenders.append(f"{t.name}:{param}")
    assert not offenders, f"parameters missing a description: {offenders}"


def test_every_tool_has_a_valid_maturity() -> None:
    valid = {"implemented", "partial", "planned"}
    offenders = [t.name for t in TOOLS if (t.meta or {}).get("maturity") not in valid]
    assert not offenders, f"tools with missing/invalid maturity: {offenders}"


def test_destructive_hint_matches_reviewed_set() -> None:
    hinted = {t.name for t in TOOLS if (t.annotations and t.annotations.destructiveHint)}
    assert hinted == _docmeta.DESTRUCTIVE_TOOLS, (
        f"destructiveHint set {sorted(hinted)} != reviewed set {sorted(_docmeta.DESTRUCTIVE_TOOLS)}"
    )


def test_gate_callers_are_in_the_destructive_set() -> None:
    # Backstop: any wrapper whose body reaches assert_destructive_allowed must be
    # in the reviewed set (the converse — admin-gated ops — is not asserted).
    gate_callers = {t.name for t in TOOLS if "assert_destructive_allowed" in _callees(t.fn)}
    assert gate_callers <= _docmeta.DESTRUCTIVE_TOOLS, (
        f"gate-calling tools not in the destructive set: "
        f"{sorted(gate_callers - _docmeta.DESTRUCTIVE_TOOLS)}"
    )


def test_implemented_tools_have_a_covering_test() -> None:
    sources = _test_sources()
    offenders: list[str] = []
    for t in TOOLS:
        if (t.meta or {}).get("maturity") != "implemented":
            continue
        anchor = _unique_anchor(t, FREQ)
        if anchor not in sources:
            offenders.append(f"{t.name} (anchor {anchor})")
    assert not offenders, (
        f"implemented tools with no non-live test referencing their callee: {offenders} "
        f"— add a test or downgrade maturity to 'partial'"
    )
