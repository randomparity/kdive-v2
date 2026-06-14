# Agent-facing Tool Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a layered agent-facing guide under `docs/guide/` whose per-namespace tool reference is generated from the live FastMCP registry, with code-resident metadata (descriptions, maturity, annotations) that feeds both the docs and the live `tools/list` schema, guarded against drift in CI.

**Architecture:** Each `@app.tool` wrapper gains a docstring, `Annotated[..., Field(description=...)]` params, a `meta={"maturity": ...}` marker, and explicit MCP `annotations` built by a small `_docmeta` helper. A generator script reads `app.list_tools()` and emits Markdown; a pytest guard asserts metadata completeness, the destructive-hint↔reviewed-set match, and a coverage floor for `implemented` tools; a `just docs-check` recipe in `just ci` fails on drift. Hand-authored concept pages complete the layer.

**Tech Stack:** Python 3.13, `uv`, FastMCP 3.4.0 (`app.list_tools()` → `FunctionTool` with `.name`/`.description`/`.parameters`/`.annotations`/`.meta`/`.fn`), `mcp.types.ToolAnnotations`, pytest, `just`, `ast`/`inspect` for the coverage floor.

**Spec:** [`../specs/2026-06-05-agent-facing-tool-guide-design.md`](../specs/2026-06-05-agent-facing-tool-guide-design.md) · **ADR:** [`../../adr/0047-agent-facing-tool-guide-generation.md`](../../adr/0047-agent-facing-tool-guide-generation.md)

---

## File structure

| File | Responsibility |
|------|----------------|
| `src/kdive/mcp/tools/_docmeta.py` (create) | The single source for annotation constructors (`read_only`/`destructive`/`mutating`), the `Maturity` literal, and the `DESTRUCTIVE_TOOLS` frozenset the guard reads |
| `src/kdive/mcp/tools/*.py` (modify, all 13) | Backfill each `@app.tool` wrapper: docstring, `Field` param descriptions, `meta` maturity, `annotations` |
| `tests/mcp/test_tool_docs.py` (create) | The guard: completeness, destructive-set match, coverage floor — over the live registry |
| `scripts/gen_tool_reference.py` (create) | Generator: registry → per-namespace Markdown under `docs/guide/reference/` |
| `tests/scripts/test_gen_tool_reference.py` (create) | Unit tests for the generator's pure core over a fake registry |
| `docs/guide/reference/*.md` (generated) | The committed, generated reference |
| `docs/guide/*.md` (create, 6) | Hand-authored concept pages |
| `justfile` (modify) | `docs` / `docs-check` recipes; add `docs-check` to `ci` |

## Reviewed classification table (the design data the backfill applies)

`destructiveHint=true` is **exactly** these 4 (the guard enforces this set — do not add or drop without updating `DESTRUCTIVE_TOOLS`):
`control.power`, `control.force_crash`, `systems.teardown`, `systems.reprovision`.

Maturity + annotation class per tool. Annotation class: **RO** = `read_only()`, **DES** = `destructive()`, **MUT** = `mutating()`. Maturity: **impl** = `implemented`, **part** = `partial`.

| Tool | Maturity | Class | Tool | Maturity | Class |
|------|----------|-------|------|----------|-------|
| allocations.request | impl | MUT | debug.start_session | part | MUT |
| allocations.get | impl | RO | debug.end_session | part | MUT |
| allocations.release | impl | MUT | debug.set_breakpoint | part | MUT |
| allocations.renew | impl | MUT | debug.clear_breakpoint | part | MUT |
| allocations.list | impl | RO | debug.list_breakpoints | part | RO |
| accounting.estimate | impl | RO | debug.read_memory | part | RO |
| accounting.usage | impl | RO | debug.read_registers | part | RO |
| accounting.report | impl | RO | debug.continue | part | MUT |
| accounting.set_budget | impl | MUT | debug.interrupt | part | MUT |
| accounting.set_quota | impl | MUT | control.power | part | DES |
| investigations.open | impl | MUT | control.force_crash | part | DES |
| investigations.get | impl | RO | systems.provision | part | MUT |
| investigations.close | impl | MUT | systems.get | impl | RO |
| investigations.link | impl | MUT | systems.teardown | part | DES |
| investigations.unlink | impl | MUT | systems.reprovision | part | DES |
| jobs.get | impl | RO | runs.create | impl | MUT |
| jobs.wait | impl | RO | runs.get | impl | RO |
| jobs.cancel | impl | MUT | runs.build | part | MUT |
| jobs.list | impl | RO | runs.install | part | MUT |
| resources.list | impl | RO | runs.boot | part | MUT |
| resources.describe | impl | RO | vmcore.fetch | part | MUT |
| artifacts.list | part | RO | vmcore.list | part | RO |
| artifacts.get | part | RO | introspect.from_vmcore | part | RO |
| postmortem.crash | part | RO | introspect.run | part | RO |
| postmortem.triage | part | RO | | | |

**Maturity rule the floor enforces:** an `implemented` tool's wrapper callee must be referenced by a non-`live_vm`/`live_stack` test. If the floor (Task 3) fails for a tool marked `impl`, **downgrade it to `part`** — under-claiming is safe and conservative. Do not mark a tool `impl` to silence the floor by adding a fake test.

---

### Task 1: The `_docmeta` annotation helper

**Files:**
- Create: `src/kdive/mcp/tools/_docmeta.py`
- Test: `tests/mcp/test_docmeta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_docmeta.py
"""_docmeta: annotation constructors + the reviewed destructive set."""

from __future__ import annotations

from kdive.mcp.tools import _docmeta


def test_read_only_sets_only_read_hint() -> None:
    a = _docmeta.read_only()
    assert a.readOnlyHint is True
    assert a.destructiveHint is not True


def test_destructive_sets_destructive_not_readonly() -> None:
    a = _docmeta.destructive()
    assert a.destructiveHint is True
    assert a.readOnlyHint is not True


def test_mutating_is_not_readonly_not_destructive() -> None:
    a = _docmeta.mutating()
    assert a.readOnlyHint is not True
    assert a.destructiveHint is not True


def test_destructive_tools_set_is_exactly_the_four() -> None:
    assert _docmeta.DESTRUCTIVE_TOOLS == frozenset(
        {
            "control.power",
            "control.force_crash",
            "systems.teardown",
            "systems.reprovision",
        }
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_docmeta.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.mcp.tools._docmeta'`

- [ ] **Step 3: Write the implementation**

```python
# src/kdive/mcp/tools/_docmeta.py
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
    }
)


def read_only() -> ToolAnnotations:
    """A query with no side effects."""
    return ToolAnnotations(readOnlyHint=True)


def destructive() -> ToolAnnotations:
    """A destructive-administration op (must be in `DESTRUCTIVE_TOOLS`)."""
    return ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def mutating() -> ToolAnnotations:
    """A state-mutating op that is not destructive-administration."""
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_docmeta.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check src/kdive/mcp/tools/_docmeta.py tests/mcp/test_docmeta.py
uv run ty check
git add src/kdive/mcp/tools/_docmeta.py tests/mcp/test_docmeta.py
git commit -m "feat(docs): add _docmeta annotation helper + reviewed destructive set"
```

---

### Task 2: The guard test (written before the backfill — it fails red)

This is the TDD anchor. It asserts over the live registry and **fails until the backfill (Tasks 4–7) is complete**. Write it now so the backfill has a target.

**Files:**
- Create: `tests/mcp/test_tool_docs.py`

- [ ] **Step 1: Write the guard test in full**

```python
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
from pathlib import Path

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.tools import _docmeta
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"
# Common callees every wrapper names; never a tool-unique anchor.
_SHARED_CALLEES = frozenset({"current_context"})


def _build_tools() -> list:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier)
    return asyncio.run(app.list_tools())


def _callees(fn: object) -> set[str]:
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


def _unique_anchor(tool, freq: Counter[str]) -> str:
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
    offenders = [
        t.name for t in TOOLS if (t.meta or {}).get("maturity") not in valid
    ]
    assert not offenders, f"tools with missing/invalid maturity: {offenders}"


def test_destructive_hint_matches_reviewed_set() -> None:
    hinted = {t.name for t in TOOLS if (t.annotations and t.annotations.destructiveHint)}
    assert hinted == _docmeta.DESTRUCTIVE_TOOLS, (
        f"destructiveHint set {sorted(hinted)} != reviewed set "
        f"{sorted(_docmeta.DESTRUCTIVE_TOOLS)}"
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
```

- [ ] **Step 2: Run it to confirm it fails (backfill not done yet)**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py -q`
Expected: FAIL — `test_every_tool_has_a_description` lists all 49 tools (wrappers are bare today), and `test_destructive_hint_matches_reviewed_set` shows an empty hinted set.

- [ ] **Step 3: Commit the red guard**

```bash
git add tests/mcp/test_tool_docs.py
git commit -m "test(docs): add tool-docs guard (red until backfill lands)"
```

---

### Task 3: Backfill `runs.py` (the fully-worked reference example)

Apply this exact pattern. Every other namespace (Tasks 4–6) repeats it using the table above and each handler's existing module docstring for the prose. `runs.*` rows from the table: `runs.create` impl/MUT, `runs.get` impl/RO, `runs.build`/`install`/`boot` part/MUT.

**Files:**
- Modify: `src/kdive/mcp/tools/runs.py` (imports near top; the `register()` block at `runs.py:625-654`)

- [ ] **Step 1: Add imports**

At the top of `runs.py`, with the other imports:

```python
from typing import Annotated  # add to the existing typing import if present
from pydantic import Field
from kdive.mcp.tools import _docmeta
```

- [ ] **Step 2: Rewrite the `register()` wrappers with metadata**

Replace the bodies in `register()` (`runs.py:628-654`) with:

```python
    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Render a Run; a failed Run maps to a failure envelope. Requires project membership."""
        return await get_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        system_id: Annotated[str, Field(description="Ready System (active Allocation) to bind.")],
        build_profile: Annotated[dict[str, Any], Field(description="Build profile for the Run's kernel.")],
    ) -> ToolResponse:
        """Bind a Run to a ready System and Investigation in one transaction. Requires operator."""
        return await create_run(
            pool,
            current_context(),
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
        )

    @app.tool(
        name="runs.build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_build(
        run_id: Annotated[str, Field(description="The Run to build.")],
    ) -> ToolResponse:
        """Enqueue the kernel build job for a Run; poll jobs.* for completion. Requires operator."""
        return await build_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
    ) -> ToolResponse:
        """Enqueue the install job for a built Run; poll jobs.* for completion. Requires operator."""
        return await install_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
    ) -> ToolResponse:
        """Enqueue the boot job for an installed Run; poll jobs.* for completion. Requires operator."""
        return await boot_run(pool, current_context(), run_id)
```

- [ ] **Step 3: Verify the runs.* tools now pass description/maturity checks**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py::test_every_tool_has_a_description -q`
Expected: still FAILs, but the offender list no longer contains any `runs.*` entry.

- [ ] **Step 4: Lint, type, commit**

```bash
uv run ruff check src/kdive/mcp/tools/runs.py
uv run ty check
git add src/kdive/mcp/tools/runs.py
git commit -m "feat(docs): backfill runs.* tool metadata"
```

---

### Task 4: Backfill the core/spine namespaces (implemented)

Apply the Task 3 pattern to each tool in these files, using the table's maturity + class and the handler's behavior for the docstring/param prose. These are all `implemented`.

**The pattern, restated (identical structure to Task 3 for every tool):** add the three imports to the file (`Annotated`, `from pydantic import Field`, `from kdive.mcp.tools import _docmeta`); on each `@app.tool(...)` add `annotations=_docmeta.<RO|DES|MUT class>()` and `meta={"maturity": "<value>"}` from the table; give the wrapper a one-line docstring stating the RBAC role + precondition (lift it from the handler's existing module docstring — it is documented there, e.g. `allocations.py`'s header); wrap every parameter as `Annotated[T, Field(description="…")]`. No behavior changes — only metadata.

**Files (modify each, in its `register()` block):**
- `allocations.py` — request(MUT) get(RO) release(MUT) renew(MUT) list(RO)
- `accounting.py` — estimate(RO) usage(RO) report(RO) set_budget(MUT) set_quota(MUT)
- `investigations.py` — open(MUT) get(RO) close(MUT) link(MUT) unlink(MUT)
- `jobs.py` — get(RO) wait(RO) cancel(MUT) list(RO)
- `resources.py` — list(RO) describe(RO)
- `systems.py` — **only** `systems.get` here (RO, implemented); the rest in Task 5

For each tool: add `annotations=_docmeta.<class>()`, `meta={"maturity": "implemented"}`, a one-line docstring (state the RBAC role and precondition from the handler), and `Annotated[T, Field(description=...)]` on every parameter. Add the `Annotated`/`Field`/`_docmeta` imports per file (as in Task 3 Step 1).

- [ ] **Step 1: Backfill `allocations.py`, then run the description check**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py::test_every_tool_has_a_description -q`
Expected: FAIL, but no `allocations.*` in the offender list.

- [ ] **Step 2: Backfill `accounting.py`, `investigations.py`, `jobs.py`, `resources.py`, and `systems.get`** the same way, re-running the description check after each file and confirming that file's tools leave the offender list.

- [ ] **Step 3: Lint, type, commit**

```bash
uv run ruff check src/kdive/mcp/tools/allocations.py src/kdive/mcp/tools/accounting.py src/kdive/mcp/tools/investigations.py src/kdive/mcp/tools/jobs.py src/kdive/mcp/tools/resources.py src/kdive/mcp/tools/systems.py
uv run ty check
git add -A src/kdive/mcp/tools/
git commit -m "feat(docs): backfill core/spine tool metadata (implemented)"
```

---

### Task 5: Backfill the provider/live-gated namespaces (partial)

Apply the same pattern; these are all `maturity="partial"`. **Destructive** tools (`control.power`, `control.force_crash`, `systems.teardown`, `systems.reprovision`) use `annotations=_docmeta.destructive()`.

**Files (modify):**
- `systems.py` — provision(MUT, part), teardown(DES, part), reprovision(DES, part)
- `control.py` — power(DES, part), force_crash(DES, part)
- `vmcore.py` — fetch(MUT, part), list(RO, part) — **note** `vmcore.py` also registers `postmortem.crash`/`triage` (RO, part)
- `introspect.py` — from_vmcore(RO, part), run(RO, part)
- `artifacts.py` — list(RO, part), get(RO, part)
- `debug.py` — start_session(MUT, part), end_session(MUT, part)
- `debug_ops.py` — set_breakpoint(MUT) clear_breakpoint(MUT) list_breakpoints(RO) read_memory(RO) read_registers(RO) continue(MUT) interrupt(MUT), all part

For `control.power`'s docstring, state the per-action split explicitly (the annotation is whole-tool):
```python
        """Power action on a started System. `on` is reversible (operator);
        off/cycle/reset are destructive (admin). Enqueues a power job."""
```

- [ ] **Step 1: Backfill each file**, re-running `test_every_tool_has_a_description` after each and confirming that file's tools leave the offender list.

- [ ] **Step 2: Verify the destructive hint now matches the reviewed set**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py::test_destructive_hint_matches_reviewed_set tests/mcp/test_tool_docs.py::test_gate_callers_are_in_the_destructive_set -q`
Expected: PASS (both).

- [ ] **Step 3: Lint, type, commit**

```bash
uv run ruff check src/kdive/mcp/tools/systems.py src/kdive/mcp/tools/control.py src/kdive/mcp/tools/vmcore.py src/kdive/mcp/tools/introspect.py src/kdive/mcp/tools/artifacts.py src/kdive/mcp/tools/debug.py src/kdive/mcp/tools/debug_ops.py
uv run ty check
git add -A src/kdive/mcp/tools/
git commit -m "feat(docs): backfill provider/live-gated tool metadata (partial)"
```

---

### Task 6: Make the guard go fully green

**Files:** none (verification + any maturity downgrades surfaced by the floor)

- [ ] **Step 1: Run the whole guard**

Run: `uv run python -m pytest tests/mcp/test_tool_docs.py -q`
Expected: All pass. If `test_implemented_tools_have_a_covering_test` fails, it names a tool marked `implemented` whose callee no non-live test references — **downgrade that tool to `maturity="partial"`** in its wrapper and re-run. If `_unique_anchor` hard-fails for a tool, its wrapper has zero or >1 unique callee; resolve by ensuring the wrapper delegates to exactly one tool-unique symbol (do not silence by editing the test).

- [ ] **Step 2: Run the full non-live suite to confirm no regressions**

Run: `just test`
Expected: PASS (the backfill is additive metadata; no behavior changed).

- [ ] **Step 3: Commit any downgrades**

```bash
git add -A src/kdive/mcp/tools/
git commit -m "fix(docs): downgrade maturity for tools without a covering non-live test"
```

---

### Task 7: The generator

**Files:**
- Create: `scripts/gen_tool_reference.py`
- Create: `tests/scripts/__init__.py` (empty — makes the new test dir a package)
- Test: `tests/scripts/test_gen_tool_reference.py`
- Modify: `pyproject.toml` (`[tool.pytest.ini_options]`)

- [ ] **Step 0: Make `scripts/` importable from tests**

`tests/scripts/test_gen_tool_reference.py` imports `scripts.gen_tool_reference`, but pytest sets no `pythonpath` today, so the repo root (where `scripts/` lives) is not on `sys.path`. Add it, and make the new test dir a package:

```bash
mkdir -p tests/scripts && : > tests/scripts/__init__.py
```

In `pyproject.toml`, under `[tool.pytest.ini_options]` (currently `testpaths = ["tests"]`, `addopts = "-ra"`), add:

```toml
pythonpath = ["."]
```

- [ ] **Step 1: Write the failing unit test (pure core over a fake registry)**

```python
# tests/scripts/test_gen_tool_reference.py
"""gen_tool_reference: the pure registry → markdown core."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts.gen_tool_reference import ToolDoc, render_namespace, tool_docs


@dataclass
class _FakeAnn:
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None


@dataclass
class _FakeTool:
    name: str
    description: str | None
    parameters: dict
    annotations: _FakeAnn | None
    meta: dict


def _tool(name: str, **kw) -> _FakeTool:
    return _FakeTool(
        name=name,
        description=kw.get("description", "Does a thing."),
        parameters=kw.get("parameters", {"properties": {}}),
        annotations=kw.get("annotations", _FakeAnn(readOnlyHint=True)),
        meta=kw.get("meta", {"maturity": "implemented"}),
    )


def test_tool_docs_extracts_fields() -> None:
    docs = tool_docs([_tool("runs.get")])
    assert docs == [
        ToolDoc(
            name="runs.get",
            namespace="runs",
            description="Does a thing.",
            maturity="implemented",
            read_only=True,
            destructive=False,
            params=[],
        )
    ]


def test_render_is_deterministic_and_grouped() -> None:
    docs = tool_docs([_tool("runs.get"), _tool("runs.create", meta={"maturity": "partial"})])
    md = render_namespace("runs", docs)
    assert md.index("runs.create") < md.index("runs.get")  # sorted
    assert "do not edit" in md
    assert "partial" in md and "implemented" in md


def test_missing_description_raises() -> None:
    with pytest.raises(ValueError, match="no description"):
        tool_docs([_tool("runs.get", description="")])


def test_missing_maturity_raises() -> None:
    with pytest.raises(ValueError, match="maturity"):
        tool_docs([_tool("runs.get", meta={})])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_gen_tool_reference.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.gen_tool_reference'`

- [ ] **Step 3: Write the generator**

```python
# scripts/gen_tool_reference.py
"""Generate the per-namespace tool reference from the live FastMCP registry (ADR-0047).

Run via `just docs` (write) / `just docs-check` (verify). The core is a pure
function over the registry's tool objects; it fails loudly on incomplete
metadata rather than emitting blanks.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app

_HEADER = "<!-- generated by scripts/gen_tool_reference.py; do not edit. Regenerate: just docs -->"
_REF_DIR = Path(__file__).resolve().parents[1] / "docs" / "guide" / "reference"


@dataclass(frozen=True)
class ParamDoc:
    name: str
    type: str
    required: bool
    description: str


@dataclass(frozen=True)
class ToolDoc:
    name: str
    namespace: str
    description: str
    maturity: str
    read_only: bool
    destructive: bool
    params: list[ParamDoc] = field(default_factory=list)


def _params(schema: dict) -> list[ParamDoc]:
    props = (schema or {}).get("properties", {})
    required = set((schema or {}).get("required", []))
    out: list[ParamDoc] = []
    for name, spec in props.items():
        out.append(
            ParamDoc(
                name=name,
                type=str(spec.get("type", "any")),
                required=name in required,
                description=(spec.get("description") or "").strip(),
            )
        )
    return out


def tool_docs(tools: list) -> list[ToolDoc]:
    """Pure registry → ToolDoc list; raises ValueError on incomplete metadata."""
    docs: list[ToolDoc] = []
    for t in tools:
        if not (t.description or "").strip():
            raise ValueError(f"{t.name}: tool has no description")
        maturity = (t.meta or {}).get("maturity")
        if maturity not in {"implemented", "partial", "planned"}:
            raise ValueError(f"{t.name}: missing/invalid maturity {maturity!r}")
        params = _params(t.parameters)
        for p in params:
            if not p.description:
                raise ValueError(f"{t.name}:{p.name}: parameter has no description")
        ann = t.annotations
        docs.append(
            ToolDoc(
                name=t.name,
                namespace=t.name.split(".", 1)[0],
                description=t.description.strip(),
                maturity=maturity,
                read_only=bool(ann and ann.readOnlyHint),
                destructive=bool(ann and ann.destructiveHint),
                params=params,
            )
        )
    return docs


def _badges(d: ToolDoc) -> str:
    flags = [d.maturity]
    if d.read_only:
        flags.append("read-only")
    if d.destructive:
        flags.append("destructive")
    return " · ".join(f"`{f}`" for f in flags)


def render_namespace(namespace: str, docs: list[ToolDoc]) -> str:
    lines = [_HEADER, "", f"# `{namespace}` tools", ""]
    for d in sorted(docs, key=lambda x: x.name):
        lines += [f"## `{d.name}`", "", _badges(d), "", d.description, ""]
        if d.params:
            lines += ["| Parameter | Type | Required | Description |", "|---|---|---|---|"]
            for p in sorted(d.params, key=lambda x: x.name):
                lines.append(f"| `{p.name}` | `{p.type}` | {'yes' if p.required else 'no'} | {p.description} |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_index(docs: list[ToolDoc]) -> str:
    lines = [_HEADER, "", "# Tool reference", "", "| Tool | Maturity |", "|---|---|"]
    for d in sorted(docs, key=lambda x: x.name):
        lines.append(f"| [`{d.name}`]({d.namespace}.md#{d.name.replace('.', '')}) | `{d.maturity}` |")
    return "\n".join(lines) + "\n"


def _registry_tools() -> list:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = RSAKeyPair.generate()
    verifier = JWTVerifier(public_key=kp.public_key, issuer="https://gen.local", audience="kdive")
    app = build_app(pool, verifier=verifier)
    return asyncio.run(app.list_tools())


def write_reference(out_dir: Path) -> None:
    docs = tool_docs(_registry_tools())
    out_dir.mkdir(parents=True, exist_ok=True)
    by_ns: dict[str, list[ToolDoc]] = {}
    for d in docs:
        by_ns.setdefault(d.namespace, []).append(d)
    for ns, ns_docs in by_ns.items():
        (out_dir / f"{ns}.md").write_text(render_namespace(ns, ns_docs), encoding="utf-8")
    (out_dir / "index.md").write_text(render_index(docs), encoding="utf-8")


if __name__ == "__main__":
    write_reference(_REF_DIR)
    print(f"wrote reference to {_REF_DIR}", file=sys.stderr)
```

- [ ] **Step 4: Run the unit test**

Run: `uv run python -m pytest tests/scripts/test_gen_tool_reference.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Generate the committed reference**

Run: `uv run python scripts/gen_tool_reference.py`
Then: `ls docs/guide/reference/` — expect one `.md` per namespace plus `index.md`.

- [ ] **Step 6: Lint, type, commit**

```bash
uv run ruff check scripts/gen_tool_reference.py tests/scripts/test_gen_tool_reference.py
uv run ty check
git add scripts/gen_tool_reference.py tests/scripts/ docs/guide/reference/
git commit -m "feat(docs): add tool-reference generator + committed reference"
```

---

### Task 8: `just docs` / `just docs-check` recipes + CI wiring

**Files:**
- Modify: `justfile` (add two recipes; add `docs-check` to the `ci` dependency list at `justfile:165`)

- [ ] **Step 1: Add the recipes**

Append to `justfile`:

```just
# Regenerate the agent-facing tool reference from the live registry (mutating).
docs:
    uv run python scripts/gen_tool_reference.py

# Verify the committed tool reference matches a fresh generation (CI gate).
docs-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv run python -c "from scripts.gen_tool_reference import write_reference; from pathlib import Path; write_reference(Path('$tmp'))"
    if ! diff -ru docs/guide/reference "$tmp"; then
        echo "tool reference is stale — run 'just docs' and commit" >&2
        exit 1
    fi
```

- [ ] **Step 2: Add `docs-check` to the `ci` recipe**

Change `justfile:165` from:
```just
ci: lint type lock-check lint-shell lint-workflows check-mermaid test
```
to:
```just
ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check test
```

- [ ] **Step 3: Verify the gate passes against the committed reference**

Run: `just docs-check`
Expected: exit 0, no diff output.

- [ ] **Step 4: Verify the gate catches drift**

Run: `printf '\n' >> docs/guide/reference/index.md && just docs-check; echo "exit=$?"; git checkout docs/guide/reference/index.md`
Expected: non-zero exit with "tool reference is stale", then the checkout restores the file.

- [ ] **Step 5: Lint the recipes and commit**

```bash
shellcheck <(sed -n '/^docs-check:/,/^$/p' justfile) || true
git add justfile
git commit -m "build(docs): add just docs/docs-check and gate ci on reference drift"
```

---

### Task 9: The hand-authored concept pages

**Files (create):** `docs/guide/index.md`, `concepts.md`, `response-envelope.md`, `async-jobs.md`, `safety-and-rbac.md`, `errors.md`

Each page is prose citing the ADR(s) from the spec's Component 3 table. Source the content from the cited ADRs and the top-level design — do not restate parameters (those live in the generated reference).

- [ ] **Step 1: Write `index.md`** — what KDIVE is, the build→boot→debug premise, one paragraph on how an agent drives the tools, and a link to `reference/index.md`. Cite `../specs/top-level-design.md`.

- [ ] **Step 2: Write `concepts.md`** — the six durable objects and lifecycle ordering (`Resource ─< Allocation ─< System ─< Run ─< DebugSession`, plus `Investigation`). Cite ADR-0003, ADR-0026.

- [ ] **Step 3: Write `response-envelope.md`** — the `ToolResponse` shape (id, status, `suggested_next_actions`, artifact `refs`, `error_category` on failure) and the references-not-log-dumps rule. Cite ADR-0019.

- [ ] **Step 4: Write `async-jobs.md`** — the `{job_id, status: running}` + poll `jobs.*`/`jobs.wait` pattern; which tools are long-running. Cite ADR-0008, ADR-0018.

- [ ] **Step 5: Write `safety-and-rbac.md`** — RBAC roles, the deny-by-default destructive-op gate (scope + role + profile opt-in), secret-by-reference + redaction. Cite ADR-0020, ADR-0027, ADR-0028.

- [ ] **Step 6: Write `errors.md`** — the `ErrorCategory` taxonomy (`domain/errors.py`) and how to read/recover from a failure envelope. Cite ADR-0019.

- [ ] **Step 7: Doc-style + mermaid check, commit**

Run: `just check-mermaid` (covers all tracked Markdown) and re-read each page for the banned words (`critical`/`robust`/`comprehensive`/`elegant`; `Milestone` not `Sprint`).

```bash
git add docs/guide/index.md docs/guide/concepts.md docs/guide/response-envelope.md docs/guide/async-jobs.md docs/guide/safety-and-rbac.md docs/guide/errors.md
git commit -m "docs(guide): add hand-authored concept layer"
```

---

### Task 10: Full gate + finish

- [ ] **Step 1: Run the full PR gate**

Run: `just ci`
Expected: PASS — lint, type, docs-check, and the test suite (including `test_tool_docs.py`) all green.

- [ ] **Step 2: Push and open the PR** (per project convention — feature branch, not main)

```bash
git push -u origin docs/agent-facing-tool-guide
gh pr create --fill --base main
```
