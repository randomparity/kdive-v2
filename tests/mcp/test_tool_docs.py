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
import re
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
# Includes _docmeta annotation helpers that appear in @app.tool decorator sources.
_SHARED_CALLEES = frozenset({"current_context", "read_only", "mutating", "destructive"})


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


def _reaches_symbol(fn: Callable[..., Any], target: str, *, depth: int = 5) -> bool:
    """Whether ``fn`` calls ``target`` directly or via a module-local delegate it calls.

    The `@app.tool` wrappers are 1:1 delegators: the security-relevant call
    (``assert_destructive_allowed``) lives one frame deeper, in the module-level handler the
    wrapper invokes (`force_crash_system`, `reprovision_system`), never in the wrapper body.
    Parsing only ``fn`` would miss it — so follow each called ``Name`` that resolves to a
    function in ``fn``'s own module globals (a nested closure still carries its module's
    globals), bounded by ``depth`` and a visited set against recursion.
    """
    seen: set[Any] = set()

    def _walk(f: Callable[..., Any], budget: int) -> bool:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(f)))
        except (OSError, TypeError):
            return False
        glb = getattr(f, "__globals__", {})
        local_calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func
                if isinstance(callee, ast.Name):
                    if callee.id == target:
                        return True
                    local_calls.add(callee.id)
                elif isinstance(callee, ast.Attribute) and callee.attr == target:
                    return True
        if budget <= 0:
            return False
        for name in local_calls:
            delegate = glb.get(name)
            if inspect.isfunction(delegate) and delegate not in seen:
                seen.add(delegate)
                if _walk(delegate, budget - 1):
                    return True
        return False

    return _walk(fn, depth)


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
        if "pytest.mark.live_vm" in text or "pytest.mark.live_stack" in text:
            continue
        blobs.append(text)
    return "\n".join(blobs)


TOOLS = _build_tools()
FREQ: Counter[str] = Counter(c for t in TOOLS for c in _callees(t.fn) if c not in _SHARED_CALLEES)


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


def _gate_reachers() -> set[str]:
    """Tools whose wrapper reaches ``assert_destructive_allowed`` (through its delegate)."""
    return {t.name for t in TOOLS if _reaches_symbol(t.fn, "assert_destructive_allowed")}


def test_gate_callers_are_in_the_destructive_set() -> None:
    # Backstop: any tool that reaches assert_destructive_allowed must be in the reviewed
    # set (the converse — admin-gated ops — is not asserted). The reach is transitive: the
    # gate lives in the module-level handler the wrapper delegates to, not in the wrapper.
    gate_reachers = _gate_reachers()
    assert gate_reachers <= _docmeta.DESTRUCTIVE_TOOLS, (
        f"gate-calling tools not in the destructive set: "
        f"{sorted(gate_reachers - _docmeta.DESTRUCTIVE_TOOLS)}"
    )


def test_backstop_actually_detects_the_known_gate_callers() -> None:
    # Canary against a vacuous backstop: the two tools that gate today must be detected as
    # gate-reachers. This fails if the reach analysis stops at the wrapper body (the gate
    # call is one delegate deeper), which would silently make the backstop above trivially
    # true and let a newly-gated op ship without destructiveHint.
    gate_reachers = _gate_reachers()
    assert {"control.force_crash", "systems.reprovision"} <= gate_reachers, (
        f"backstop failed to detect known gate-callers; saw {sorted(gate_reachers)}"
    )


def test_implemented_tools_have_a_covering_test() -> None:
    sources = _test_sources()
    offenders: list[str] = []
    for t in TOOLS:
        if (t.meta or {}).get("maturity") != "implemented":
            continue
        anchor = _unique_anchor(t, FREQ)
        # Word-boundary, not substring: anchor `get_run` must not be satisfied by an
        # unrelated `get_run_summary` token (the anchor is a whole symbol reference).
        if not re.search(rf"\b{re.escape(anchor)}\b", sources):
            offenders.append(f"{t.name} (anchor {anchor})")
    assert not offenders, (
        f"implemented tools with no non-live test referencing their callee: {offenders} "
        f"— add a test or downgrade maturity to 'partial'"
    )
