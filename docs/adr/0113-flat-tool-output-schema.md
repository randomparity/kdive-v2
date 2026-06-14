# ADR 0113 — Advertise a flat tool `outputSchema` to stop the recursive-schema client error (#404)

- **Status:** Proposed
- **Date:** 2026-06-14
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0019](0019-tool-response-envelope.md) (the uniform
  `ToolResponse` envelope every tool returns — unchanged at the payload level by this ADR),
  [ADR-0044](0044-mcp-wire-harness-oidc-token-issuance.md) (the live-stack client's
  `structured_content` parsing contract — also unchanged).
- **Issue:** [#404](https://github.com/randomparity/kdive/issues/404).
- **Spec:** [`../superpowers/specs/2026-06-14-flat-tool-output-schema.md`](../archive/superpowers/specs/2026-06-14-flat-tool-output-schema.md).

## Context

Every MCP tool returns `ToolResponse` (`src/kdive/mcp/responses.py`), which is self-referential:
`items: list[ToolResponse]` is a direct self-reference and `data: dict[str, JsonValue]` uses the
recursive `JsonValue` union. FastMCP auto-derives each tool's `outputSchema` from the return
annotation, producing a schema with self-referential `$ref`s. The FastMCP 3.4.0 **client** builds
a `TypeAdapter` from the advertised `outputSchema` on every `call_tool` result; the recursive
`$ref` makes that build raise (`maximum recursion depth exceeded` / `TypeAdapter[ForwardRef('Root')]`
is not fully defined). Verified against `fastmcp==3.4.0` with an in-memory `Client`, this has two
effects: a per-call ERROR log on every tool call, and `CallToolResult.data` returning `None` (only
the raw `structured_content` dict survives). The kdive `LiveStackClient` already routes around the
broken `.data` by reading `structured_content` directly.

The bug is non-fatal (the client falls back) but noisy in the live-stack/spine logs and an interop
hazard for a stricter client that does not fall back. The constraint is to keep the runtime
`ToolResponse` payload and its `structured_content` wire shape byte-identical while removing the
recursion from the *advertised* schema.

## Decision

We will override the advertised `outputSchema` of every registered tool to the flat constant
`{"type": "object"}`, applied centrally in `build_app` after the plane registrars run, by setting
the `output_schema` attribute on each **live** tool instance in the local registry
(`app.local_provider`'s `Tool` components — not the copies `app.list_tools()` returns). The
`ToolResponse` model, the `structured_content` wire payload, and the runtime
`validate_json_value` JSON-safety check are unchanged.

## Consequences

- The per-call client "Error parsing structured content" log is gone, and `CallToolResult.data`
  is restored to the populated envelope dict.
- `structured_content` is unchanged (the flat schema carries no `x-fastmcp-wrap-result`, so
  `convert_result` still emits the unwrapped envelope dict). `LiveStackClient` and the
  `structured_content`-shape pin test are unaffected.
- Every current and future tool is covered by the single `build_app` chokepoint; a newly added
  tool cannot regress to the recursive schema.
- The sweep depends on the FastMCP-internal registry layout (`app.local_provider`'s `Tool`
  components). Because the bug is non-fatal (the client falls back to `structured_content`), a
  sweep that enumerated the wrong collection, or one a future FastMCP rename emptied, would
  silently regress. The helper therefore raises if it sweeps zero tools, and a `build_app`-backed
  end-to-end test drives the real app through a `Client` so a broken accessor fails a test rather
  than shipping.
- The advertised `outputSchema` is now uninformative about envelope fields (it says only "an
  object"). This is acceptable: the prior schema was unusable (it broke the client), runtime
  JSON-safety is still enforced by `validate_json_value`, and the input parameter schemas (the
  agent-facing contract the tool reference renders) are unchanged.
- The sweep forces a flat object schema onto any tool regardless of its return type. The
  surface-wide invariant is that every tool returns `ToolResponse` (an object), so this is
  correct for the current surface; a future tool that returned a non-object (e.g.
  `list[ToolResponse]`, which FastMCP wraps as `{"result": [...]}`) would have its wrapping
  changed by the flat override. No current tool does this; a boundary test pins the invariant so
  the assumption fails loudly if it is ever broken.

## Alternatives considered

- **`output_schema=None` instead of a flat object.** Also removes the recursion and restores
  `.data`, but advertises *no* output schema, so an introspecting client loses the "returns a
  structured object" signal. A flat `{"type": "object"}` is strictly more informative at the same
  client cost, so `None` lost.
- **Pass `output_schema={"type": "object"}` on each `@app.tool` call.** Correct but scattered
  across 96 registrations in many modules; a newly added tool would silently regress. The central
  `build_app` sweep covers everything through one stable entrypoint, so the per-call form lost on
  maintainability.
- **Override `ToolResponse.__get_pydantic_json_schema__` to emit a non-recursive model schema.**
  Fixes the derivation at the model, but changes `ToolResponse.model_json_schema()` for *every*
  consumer (broader than the bug, which is the MCP advertisement boundary), and requires
  surgery against pydantic's internal `$defs`/`$ref` output. The MCP-boundary override is
  narrower and does not touch the shared model, so the model-level rewrite lost.
- **Hand-write a full non-recursive `ToolResponse` schema** (all envelope fields, with generic
  `items`/`data`). More informative, but it must stay permissive enough that the client's
  `validate_python` never rejects a real payload, and it drifts silently whenever `ToolResponse`
  gains a field. `validate_json_value` already enforces runtime JSON-safety, so a precise
  advertised schema buys little for real drift risk; it lost.
