# Flat tool `outputSchema` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop kdive MCP tools advertising a recursive `outputSchema` (which makes the FastMCP
3.4.0 client log a parse error and null `CallToolResult.data` on every call) by sweeping every
registered tool in `build_app` to advertise a flat `{"type": "object"}`.

**Architecture:** A single chokepoint change in `src/kdive/mcp/app.py`. After the plane
registrars run, iterate the live registered `Tool` instances in `app.local_provider`'s component
store and set each tool's `output_schema` to a shared flat constant. The `ToolResponse` model and
the `structured_content` wire payload are unchanged. The sweep raises if it finds zero tools so a
future FastMCP rename of the registry accessor fails loudly instead of silently regressing.

**Tech Stack:** Python 3.13, FastMCP 3.4.0, pytest, `uv`/`just`.

**Spec:** `docs/superpowers/specs/2026-06-14-flat-tool-output-schema.md`
**ADR:** `docs/adr/0113-flat-tool-output-schema.md`

**Guardrails (run before each commit):**
`just lint` · `just type` · the focused tests below · `just docs-check`

---

## File structure

- `src/kdive/mcp/app.py` — add `ENVELOPE_OUTPUT_SCHEMA` constant + `_advertise_flat_output_schema(app)`
  helper; call the helper at the end of `build_app` (after the registrar loop, before `return app`).
- `tests/mcp/core/test_output_schema.py` — new focused unit suite (probe app: flat schema, data
  restored, no client error; recursive-auto regression; zero-count guard).
- `tests/mcp/core/test_tool_wrapper_boundary.py` — add one end-to-end assertion against the real
  `build_app` app (a representative real tool advertises the flat schema; a call logs no parse error).

---

### Task 1: Flat schema constant + sweep helper (unit, probe app)

**Files:**
- Modify: `src/kdive/mcp/app.py` (add constant + helper; wire into `build_app`)
- Test: `tests/mcp/core/test_output_schema.py` (create)

- [ ] **Step 1: Write the failing test (probe app, swept by the helper)**

Create `tests/mcp/core/test_output_schema.py`:

```python
"""The flat-outputSchema sweep that fixes the recursive ToolResponse schema (#404, ADR-0113)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP

from kdive.mcp.app import ENVELOPE_OUTPUT_SCHEMA, _advertise_flat_output_schema
from kdive.mcp.responses import ToolResponse


def _probe_app() -> FastMCP:
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="scalar.one")
    def scalar_one() -> ToolResponse:
        return ToolResponse.success("obj-1", "ok", data={"k": "v"})

    @app.tool(name="list.coll")
    def list_coll() -> ToolResponse:
        return ToolResponse.collection("c", "ok", [ToolResponse.success("a", "ok")])

    return app


class _ErrorCollector(logging.Handler):
    """Capture ERROR records off the ``fastmcp`` logger.

    The FastMCP client logger sets ``propagate=False`` and uses its own handler, so pytest's
    ``caplog`` (a root-logger handler) does NOT see the parse error — verified. Attach directly.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _call_and_capture(app: FastMCP, tool: str) -> tuple[object, list[str]]:
    """Call ``tool`` on ``app``; return (``.data``, structured-content error messages)."""
    logger = logging.getLogger("fastmcp")
    handler = _ErrorCollector()
    logger.addHandler(handler)
    try:

        async def _call() -> object:
            async with Client(app) as c:
                res = await c.call_tool(tool, {})
                return res.data

        data = asyncio.run(_call())
    finally:
        logger.removeHandler(handler)
    errors = [r.getMessage() for r in handler.records if "structured content" in r.getMessage()]
    return data, errors


def test_sweep_advertises_flat_object_schema() -> None:
    app = _probe_app()
    swept = _advertise_flat_output_schema(app)
    assert swept == 2

    async def _run() -> list[dict[str, object] | None]:
        async with Client(app) as c:
            return [t.outputSchema for t in await c.list_tools()]

    schemas = asyncio.run(_run())
    assert schemas == [ENVELOPE_OUTPUT_SCHEMA, ENVELOPE_OUTPUT_SCHEMA]


def test_sweep_restores_data_and_logs_no_parse_error() -> None:
    app = _probe_app()
    _advertise_flat_output_schema(app)
    data, errors = _call_and_capture(app, "scalar.one")
    assert isinstance(data, dict)
    assert data["object_id"] == "obj-1"  # S1b: .data restored
    assert errors == []  # S1a: no parse-error log


def test_unswept_recursive_schema_fails_to_parse() -> None:
    """Regression pin: without the sweep the auto-derived recursive schema breaks the client.

    Pinned to fastmcp 3.4.0 client behavior; a major FastMCP upgrade that handles recursive
    ``$ref`` would make this auto-schema parse cleanly and is the expected reason to revisit it.
    """
    app = _probe_app()  # NOT swept
    data, errors = _call_and_capture(app, "scalar.one")
    assert data is None  # the failed validator nulls .data
    assert errors  # the parse error is logged


def test_sweep_raises_on_empty_tool_surface() -> None:
    """A zero count means the registry accessor broke — fail loud, don't ship recursive schemas."""
    empty: FastMCP = FastMCP(name="empty")
    with pytest.raises(RuntimeError):
        _advertise_flat_output_schema(empty)
```

- [ ] **Step 2: Run the tests, verify they fail for the right reason**

Run: `uv run python -m pytest tests/mcp/core/test_output_schema.py -q`
Expected: collection/import error — `ENVELOPE_OUTPUT_SCHEMA` / `_advertise_flat_output_schema`
do not exist in `kdive.mcp.app`.

- [ ] **Step 3: Implement the constant + helper, wire into `build_app`**

In `src/kdive/mcp/app.py`:

Add the import near the other fastmcp imports:

```python
from fastmcp.tools import Tool
```

Add the constant and helper above `build_app` (module level):

```python
# A flat, non-recursive output schema advertised for every tool (ADR-0113). Every tool returns
# the self-referential ``ToolResponse``; FastMCP would auto-derive a recursive ``$ref`` schema
# that the FastMCP 3.4.0 client cannot build a validator for (it logs a per-call parse error and
# nulls ``CallToolResult.data``). Advertising a flat object removes the recursion while keeping
# the ``structured_content`` wire payload unchanged (no ``x-fastmcp-wrap-result`` key).
ENVELOPE_OUTPUT_SCHEMA: dict[str, str] = {"type": "object"}


def _advertise_flat_output_schema(app: FastMCP) -> int:
    """Override every registered tool's advertised ``outputSchema`` with the flat envelope schema.

    Mutates the *live* registered ``Tool`` instances (the ``Tool``-typed values in the local
    provider's component store) — ``app.list_tools()`` returns copies whose mutation would not
    change what the server advertises. Raises if no tools are found: ``build_app`` always
    registers a non-empty surface, so a zero count means the registry accessor changed under us
    and the app must not silently fall back to advertising the recursive schema (ADR-0113).

    Returns the number of tools swept.
    """
    swept = 0
    for component in app.local_provider._components.values():
        if isinstance(component, Tool):
            component.output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)
            swept += 1
    if swept == 0:
        raise RuntimeError(
            "no tools found to advertise a flat outputSchema for; the FastMCP registry "
            "accessor (app.local_provider._components) may have changed (ADR-0113)"
        )
    return swept
```

In `build_app`, after the registrar loop and before `return app`:

```python
    for register in _PLANE_REGISTRARS:
        register(app, pool, assembly)
    _advertise_flat_output_schema(app)
    return app
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run python -m pytest tests/mcp/core/test_output_schema.py -q`
Expected: 4 passed.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/mcp/core/test_output_schema.py -q`
Expected: all green.

```bash
git add src/kdive/mcp/app.py tests/mcp/core/test_output_schema.py
git commit -m "fix(mcp): advertise flat tool outputSchema to stop recursive-schema client error (#404)"
```

---

### Task 2: End-to-end boundary assertion against the real `build_app`

**Files:**
- Test: `tests/mcp/core/test_tool_wrapper_boundary.py` (add one test)

- [ ] **Step 1: Write the failing test**

Add to `tests/mcp/core/test_tool_wrapper_boundary.py` (reuse the file's existing `build_app`
fixture/helpers — inspect the top of the file for how it constructs the app and a `Client`; the
new test asserts a representative real tool advertises the flat schema and a call logs no parse
error). Skeleton (adapt to the file's existing app-construction helper, named `<existing>` below):

Preferred form — assert the advertised-schema half against the **real** `build_app` app (no DB
needed; the sweep runs inside `build_app`, so just build and introspect). Use the file's existing
`build_app(...)` construction helper:

```python
def test_real_build_app_tools_advertise_flat_output_schema() -> None:
    """Every build_app tool advertises the flat envelope schema (#404, end-to-end enumeration)."""
    app = _build_real_app()  # the file's existing build_app(...) helper/fixture

    async def _schemas() -> list[dict[str, object] | None]:
        async with Client(app) as c:
            return [t.outputSchema for t in await c.list_tools()]

    schemas = asyncio.run(_schemas())
    assert schemas, "build_app registered no tools"
    assert all(s == {"type": "object"} for s in schemas)
```

This exercises `build_app`'s real enumeration (a renamed registry accessor → `build_app` raises
via the zero-count guard, or the schemas are non-flat → this assertion fails). The call-time
log-silence path is already covered by the probe suite in Task 1, so a DB-backed authed
`call_tool` here is not required.

- [ ] **Step 2: Run it, verify it fails (or passes) for the right reason**

Run: `uv run python -m pytest tests/mcp/core/test_tool_wrapper_boundary.py -q -k output_schema`
Expected before the Task 1 change is present: FAIL (recursive schema). With Task 1 merged: PASS.

- [ ] **Step 3: No implementation needed (Task 1 already implements the behavior)**

This task only adds end-to-end coverage that exercises `build_app`'s real enumeration.

- [ ] **Step 4: Run the full focused suite + guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/core/test_tool_wrapper_boundary.py tests/mcp/core/test_output_schema.py tests/integration/live_stack/test_client_inmemory.py -q && just docs-check`
Expected: all green (the `structured_content` shape pin in `test_client_inmemory.py` must still pass).

- [ ] **Step 5: Commit**

```bash
git add tests/mcp/core/test_tool_wrapper_boundary.py
git commit -m "test(mcp): assert real build_app tools advertise flat outputSchema (#404)"
```

---

## Rollback / cleanup

- The change is additive and confined to `app.py` + tests. To roll back, drop the
  `_advertise_flat_output_schema(app)` call (the recursive schema returns; behavior reverts to the
  non-fatal pre-fix state). No data, schema, or migration state is touched.
- No committed snapshot/approval files are affected (`docs-check` is input-parameter only).
