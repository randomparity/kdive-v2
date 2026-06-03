# ADR 0019 — Uniform tool-response envelope

- **Status:** Proposed
- **Date:** 2026-06-03
- **Refines:** [0010](0010-fastmcp-framework-auth.md) ("every tool must return
  structured JSON with object id, status, `suggested_next_actions`, and artifact
  references — never log dumps") and the spec's "MCP tool surface".

## Context

[0010](0010-fastmcp-framework-auth.md) mandates that *every* MCP tool — across
discovery, allocation, provisioning, build, debug, control, retrieve, and jobs —
returns structured JSON carrying an object id, a `status`, a
`suggested_next_actions` list, and artifact **references**, never raw log dumps.
The M0 plan surfaces each plane through a `register(app)` hook so plane issues add
tools without editing each other. If each plane invents its own return shape, the
"learn one polling pattern" promise (spec, "Job queue") and the uniform agent
contract erode immediately, and the redaction guarantee (references not dumps)
becomes per-plane discipline rather than one enforced shape.

The jobs tools are the first to land (issue #10). The spec gives them a flat
`{job_id, kind, status, result_ref?, error_category?}` sketch, but the same issue's
acceptance restates the requirement generically as "object id, status,
`suggested_next_actions`, refs". We need one envelope that both satisfies and that
the later planes reuse unchanged.

## Decision

We will define one `ToolResponse` Pydantic model in `src/kdive/mcp/responses.py`
that every tool returns. Its fields are exactly the cross-cutting four plus a
failure slot and a small typed escape hatch for plane scalars:

- `object_id: str` — the primary object's id (the `job_id` for `jobs.*`).
- `status: str` — the object's lifecycle status as a plain string.
- `suggested_next_actions: list[str]` — literal next **tool names** (e.g.
  `"jobs.wait"`), so an agent can act without inferring the next call.
- `refs: dict[str, str]` — artifact **references** keyed by role (e.g.
  `{"result": "<object-store-key>"}`); never inline artifact bytes or log text.
- `error_category: str | None` — set **iff** the response reports a failed object,
  carrying the value from the `ErrorCategory` taxonomy; `None` otherwise.
- `data: dict[str, str]` — plane-specific scalar fields that are not one of the
  above (for `jobs.*`, `{"kind": "<job-kind>"}`). Constrained to `str→str` so the
  envelope stays JSON-trivial and type-checkable; richer plane payloads that
  later need nesting get their own typed sub-model rather than loosening this.

Two constructors keep call sites uniform and the failure invariant local:
`ToolResponse.from_job(job)` builds the jobs shape, and the model rejects an
`error_category` paired with a non-failure `status` (and vice versa) at validation
time, so "category iff failed" is enforced once here rather than per tool.

## Consequences

- Every plane added through the `register(app)` hook returns the same shape; an
  agent learns one envelope and one polling pattern across the whole surface.
- "References, never dumps" is structural: there is no field for inline log text,
  so a plane cannot accidentally return one. The redaction gate (issue #23) still
  governs whether a referenced artifact is response-eligible; this envelope only
  guarantees the *shape* carries a reference, not bytes.
- The `data: dict[str, str]` hatch is deliberately narrow. A plane that needs a
  structured payload (e.g. a list result for `*.list`) must extend the contract
  explicitly — caught in review — rather than smuggling arbitrary JSON through a
  loose field.
- Introducing the model now, before a second plane exists, is justified by
  immediate reuse: the four `jobs.*` tools all return it, and ADR-0010 already
  fixed the shape as a surface-wide requirement, so this is the agreed contract,
  not a speculative one.
- `*.list` returns a sequence of objects, which a single `ToolResponse` does not
  model. M0's `jobs.list` returns `list[ToolResponse]` (one envelope per job); a
  paginated list envelope is deferred until a plane needs cursors.

## Alternatives considered

- **No shared model; each tool returns a plain `dict`.** Rejected: the uniform
  shape and the "references, never dumps" rule then live only in prose and
  per-plane review, exactly what ADR-0010 set out to avoid. Drift is a matter of
  time once a second plane lands.
- **A jobs-only `JobHandle` now, generalize later.** Rejected: ADR-0010 already
  fixed the surface-wide shape, the register hook is explicitly built for many
  planes, and "extract the envelope when the second plane lands" pays an avoidable
  migration across every already-shipped tool. The reuse is not speculative.
- **A fully generic `data: Mapping[str, Any]` payload, no typed cross-cutting
  fields.** Rejected: it makes `object_id`/`status`/`suggested_next_actions`
  optional-by-convention and unenforceable, and `ty` cannot check it. The whole
  point is that the four fields are guaranteed.
- **Flat per-plane shapes matching each spec sketch verbatim (`{job_id, kind,
  …}`).** Rejected: `job_id`/`system_id`/`run_id` as distinct top-level keys means
  an agent's polling code branches per plane; `object_id` unifies them and the
  plane name is already in the tool name.
