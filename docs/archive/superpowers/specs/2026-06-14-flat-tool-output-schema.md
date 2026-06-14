# Spec — Advertise a flat tool `outputSchema` to stop the recursive-schema client error (#404)

- **Status:** Draft
- **Date:** 2026-06-14
- **Issue:** [#404](https://github.com/randomparity/kdive/issues/404)
- **ADR:** [ADR-0113](../../adr/0113-flat-tool-output-schema.md)

## Problem

Every kdive MCP tool returns `ToolResponse` (`src/kdive/mcp/responses.py`). The model is
self-referential in two ways:

- `items: list[ToolResponse]` — a direct self-reference (the collection envelope).
- `data: dict[str, JsonValue]` — `JsonValue` (`src/kdive/serialization.py`) is a recursive
  union (`... | list[JsonValue] | dict[str, JsonValue]`).

FastMCP auto-derives each tool's `outputSchema` from the return annotation. For `ToolResponse`
that schema contains self-referential `$ref`s (`#/$defs/ToolResponse`, `#/$defs/JsonValue`).
The FastMCP 3.4.0 **client** builds a `TypeAdapter` from the advertised `outputSchema` on every
`call_tool` result. The recursive `$ref` makes that build fail:

```
ERROR [Client-…] Error parsing structured content: maximum recursion depth exceeded
ERROR [Client-…] Error parsing structured content: `TypeAdapter[ForwardRef('Root')]` is not fully defined; …
```

Two observed effects (verified against `fastmcp==3.4.0`, in-memory `Client`):

1. **Per-call ERROR log.** The client logs the parse failure on *every* tool call, drowning
   real signal in the live-stack / spine logs.
2. **`CallToolResult.data` is `None`.** The failed validator means the typed `.data` accessor
   returns `None`; only the raw `structured_content` dict survives. The kdive `LiveStackClient`
   already routes around this — it reads `structured_content` directly and its docstring states
   "`CallToolResult.data` is not used".

The bug is non-fatal today (the client falls back to text/`structured_content`), but it is
noisy and an interop hazard: a stricter MCP client that does not fall back could fail outright,
and a recursive `outputSchema` is poorly supported across the ecosystem.

## Goal

Stop advertising a recursive `outputSchema` for kdive tools, while keeping the runtime
`ToolResponse` payload and its `structured_content` wire shape byte-identical.

### Success criteria (falsifiable)

- S1a. After the fix, a FastMCP 3.4.0 `Client` calling a kdive tool over the in-memory transport
  emits **no** ERROR record on the `fastmcp.client` logger (the user-visible symptom in #404 —
  the per-call "Error parsing structured content" log). Asserted directly by capturing the
  client logger, not inferred from `.data`.
- S1b. `CallToolResult.data` is the populated envelope dict (not `None`) for the same call.
- S2. Every tool registered by `build_app` advertises `outputSchema == {"type": "object"}`
  (no `$ref`, no `$defs`).
- S3. The `structured_content` wire payload is unchanged: a scalar tool's structured content is
  the envelope object dict (top-level keys `object_id`/`status`/…); the change adds nothing and
  removes nothing from it.
- S4. The runtime JSON-safety guarantee is unchanged: `ToolResponse._data_is_json_compatible`
  (`validate_json_value`) still rejects non-JSON `data`.
- S5. The generated tool reference (`just docs-check`) is unchanged — the generator renders only
  input parameters/annotations, not `outputSchema`.

## Approach

FastMCP exposes `output_schema` as a settable attribute on each registered tool (read by
`Tool.to_mcp_tool()` when building the advertised descriptor, and by `convert_result` when
deciding result wrapping). `build_app` (`src/kdive/mcp/app.py`) is the single chokepoint that
runs after every plane registrar. After the registrar loop, iterate the live registered tool
instances and set each tool's `output_schema` to a shared flat constant:

```python
ENVELOPE_OUTPUT_SCHEMA = {"type": "object"}
```

A flat object schema is accurate (the envelope *is* a JSON object), non-recursive (the client
builds a trivial `dict` validator that accepts any object), and carries no
`x-fastmcp-wrap-result` key, so `convert_result` keeps emitting the unwrapped envelope dict as
`structured_content` (wire shape unchanged).

### Enumerating the live tool instances (and why it is fragile)

The sweep must mutate the **live registered** tool instances, not copies. In FastMCP 3.4.0:

- `app.list_tools()` returns **copies** — verified: mutating their `output_schema` does not change
  what the server advertises. Using it would make the sweep a silent no-op.
- The live instances are the `Tool`-typed values in `app._local_provider._components` (a plain
  `dict[str, FastMCPComponent]`, populated synchronously by each `@app.tool` registration).
  `Tool.to_mcp_tool()` reads `output_schema` off these instances. (`app.local_provider` is the
  supported accessor for the local registry — FastMCP's own deprecation guidance points power
  users at `mcp.local_provider`; `_components` is its backing store.)

Because the bug is non-fatal (the client falls back to `structured_content`), a sweep that
enumerates the wrong collection, or that a future FastMCP rename empties, would **silently**
regress to the recursive schema with no crash and no missing data at the `LiveStackClient`
boundary. The sweep helper therefore **fails loud if it sweeps zero tools** — a zero count means
the enumeration accessor broke, and the app must not start advertising recursive schemas
unnoticed. (`build_app` always registers a non-empty tool surface, so zero is unambiguously a
defect.)

### Why flat object, not `None`

`output_schema=None` also removes the recursion, but advertises *no* output schema at all, so an
introspecting client loses the "this tool returns a structured object" signal. A flat
`{"type": "object"}` is strictly more informative at the same client cost (both restore `.data`
and silence the error), so the fix advertises the flat object. See ADR-0113 for the full
considered/rejected set.

### Where the override is applied

The override is applied **centrally** in `build_app`, not per `@app.tool` call. There are 96
registrations across the tool packages; a per-call argument would be scattered and a newly added
tool would silently regress to the recursive schema. The central sweep covers every current and
future tool through the one entrypoint that the architecture already designates as stable
("two registrar seams keep the entrypoint stable", AGENTS.md).

## Scope / blast radius

- One source change: `src/kdive/mcp/app.py` (a constant + a sweep helper called from
  `build_app`). No change to `responses.py`, `serialization.py`, or any tool module.
- The envelope contract (ADR-0019) and the live-stack parsing contract (ADR-0044) are unchanged
  at the payload level; only the advertised schema metadata changes.

## Verification

- Unit (no DB): a probe `FastMCP` app with a scalar and a collection tool, swept by the helper,
  asserts (a) advertised `outputSchema == {"type": "object"}`, (b) a `Client` call returns
  `data is not None` with the envelope keys (S1b), (c) **no ERROR on the `fastmcp.client`
  logger**, captured with `caplog` (S1a). A regression test asserts the pre-fix recursive
  auto-schema actually fails to parse (so the guard cannot go vacuous). *That regression test is
  pinned to fastmcp 3.4.0 client behavior — a major FastMCP upgrade that handles recursive `$ref`
  would make the auto-schema parse cleanly and is the expected reason to revisit it.*
- Zero-count guard: a test asserts the sweep helper **raises** when handed an app with no tools,
  so a future FastMCP rename of the registry accessor fails a test instead of silently shipping
  the recursive schema.
- Boundary (end-to-end against the real surface): a `build_app`-backed test (the existing
  wrapper-boundary suite) drives the **real** app through a `Client` and asserts a representative
  real tool advertises `outputSchema == {"type": "object"}` and its call logs no parse error —
  exercising `build_app`'s actual enumeration, not only the probe helper.
- `just lint type test docs-check` green locally.
- `live_stack` parse-clean verification is the operator runbook step (needs the running stack);
  noted in the PR body as the manual confirmation, per the issue.

## Out of scope

- Changing `ToolResponse`'s general `model_json_schema()` (the model stays recursive for other
  consumers; the bug is at the MCP advertisement boundary only).
- Pinning the FastMCP version or any upstream change.
- Per-tool precise output schemas (a future enhancement if a typed envelope schema is wanted;
  not needed to fix #404).
