# Agent-facing tool guide: layered docs generated from the registry

- **Date:** 2026-06-05
- **Status:** Proposed
- **ADR:** [`../../adr/0047-agent-facing-tool-guide-generation.md`](../../adr/0047-agent-facing-tool-guide-generation.md)
- **Top-level design:** [`../../specs/top-level-design.md`](../../specs/top-level-design.md)

## Problem

KDIVE has deep *internal* documentation — 47 ADRs, milestone specs and plans, a
runbook — all aimed at people building KDIVE. It has **no** documentation aimed at the
parties that *consume* the MCP service: the coding agent driving the tools and the human
developer pointing that agent at KDIVE. A consumer today must read the source to learn
what `runs.create` takes, what a `ToolResponse` carries, when a call needs the `operator`
role, or which tools block on a long-running job.

Two forces make a hand-written reference the wrong answer:

1. **Drift.** The tool surface is 49 tools across 13 namespaces and still moving. A
   hand-authored reference rots the moment a parameter changes, and nothing catches it.
2. **Honesty.** The surface is not uniformly real. The allocation/accounting/state-machine
   spine is implemented and unit-tested; the build → boot → crash → introspect path leans
   on provider seams that are partly stubbed (`build-guest-image.sh` is a stub; the guest
   image digest is a placeholder) and verified only under the gated `live_vm`/`live_stack`
   markers. A reference that lists every registered tool uniformly presents stubbed tools
   as production-ready — a phantom-feature trap.

## Goals

- A layered guide: a hand-authored conceptual layer for onboarding (human and agent) and a
  per-namespace tool reference for precise contracts.
- The reference is **generated from the live FastMCP registry**, so it cannot describe a
  tool that does not exist or omit one that does.
- Every tool and parameter carries a description, and every tool carries a **maturity**
  marker, so the guide never overstates what works. Presence is enforced in CI.
- The same metadata that feeds the docs feeds the **live MCP `tools/list` schema**, so a
  real agent calling KDIVE gets better tool descriptions and standard MCP hints — not only
  a reader of a Markdown file.

## Non-goals

- A published documentation site (mkdocs/Sphinx). The guide is plain Markdown under
  `docs/`, consistent with the rest of the tree. A site can be layered on later.
- Tutorials or task walkthroughs. The spine is *by namespace*; worked end-to-end journeys
  are out of scope for this pass.
- Auto-*inferring* maturity from test coverage. Maturity is a reviewed, explicit marker
  (see "Maturity marker").

## Architecture

Five units, each with one job:

| Unit | Lives in | Job |
|------|----------|-----|
| Tool metadata | `src/kdive/mcp/tools/*.py` | The source of truth: docstrings, param descriptions, maturity, annotations on each `@app.tool` wrapper |
| Generator | `scripts/gen_tool_reference.py` | Read the registry → emit per-namespace reference Markdown |
| Conceptual layer | `docs/guide/*.md` | Hand-authored onboarding the reference hangs off |
| Guard | `just docs-check` + `tests/mcp/test_tool_docs.py` | Fail on drift, missing description, or missing/invalid maturity |
| ADR-0047 | `docs/adr/` | Records the decision and the maturity criteria |

Data flow:

```
gate trigger + read/write facts
        │
        ▼
@app.tool wrapper metadata ──► live MCP tools/list schema
        │
        ▼
   generator ──► docs/guide/reference/*.md (committed)
        │
        ▼
   guard verifies the committed reference equals a fresh generation,
   and that every tool's metadata is complete
```

## Component 1 — Tool metadata in code

Each `@app.tool` wrapper gains four things. Today the wrappers are bare — no docstring,
plain-typed parameters, no metadata (see `src/kdive/mcp/tools/runs.py:628`). The backfill
is the bulk of the work and touches all 49 wrappers.

1. **Docstring** → the tool description. States what the tool does, the RBAC role it
   requires, and any precondition (e.g. "the System must be `ready`"). Prose, not a schema.
2. **`Annotated[T, Field(description=...)]`** on every parameter → parameter descriptions in
   the schema. A bare `run_id: str` becomes
   `run_id: Annotated[str, Field(description="The Run to build.")]`.
3. **`meta={"maturity": "<implemented|partial|planned>"}`** → the maturity marker.
4. **`annotations=ToolAnnotations(...)`** → the standard MCP `readOnlyHint` /
   `destructiveHint` / `idempotentHint`.

### Maturity marker

A reviewed, explicit claim co-located with the tool — *not* inferred from test coverage.
The guard enforces presence and a valid value; ADR-0047 defines the criteria a reviewer
applies, so the judgement is anchored rather than arbitrary. The enum:

- **`implemented`** — backed by a real provider op (or a core DB operation) and covered by
  non-live (unit/service) tests; works on a stock host without `live_vm`/`live_stack`.
- **`partial`** — wired end-to-end but depends on a stubbed seam, or verified only under
  the gated `live_vm`/`live_stack` markers.
- **`planned`** — registered surface whose backing op is a stub or not yet functional.

### Annotations are explicit, and the destructive hint is guarded

Annotations are **not** derived from `OpContract`. ADR-0063 makes typed
`ProviderRuntime` ports the active M0/M1 production seam, so there is no live
`CapabilityRegistry.dispatch` result or `BoundOp` to read at tool registration. Each wrapper
therefore sets its `annotations` explicitly, as a reviewed claim, across **three classes** (a
two-way "non-query ⇒ read-only" split would mislabel the third):

- **Read-only queries** — `*.get`, `*.list`, `accounting.usage`/`estimate`/`report`,
  `jobs.get`: DB reads with no side effects (`accounting.report`/`estimate` compute
  reserved/reconciled/variance and cost preflights; the *spine* writes the report artifact,
  the tool does not) → `readOnlyHint=true`.
- **Destructive-administration ops** — `control.power`, `control.force_crash`,
  `systems.teardown`, `systems.reprovision` (ADR-0028/ADR-0037) → `destructiveHint=true`.
  These split by *how* they are enforced (below), but all get the hint.
- **State-mutating, non-destructive** — `allocations.release`/`renew`,
  `accounting.set_budget`/`set_quota`, `investigations.*` writes, `jobs.cancel`: mutate
  state but are not destructive-administration → `readOnlyHint=false`, no destructive claim.

`control.power` is one tool spanning a reversible `power on` (`operator`) and the destructive
`off`/`cycle`/`reset` (`admin`), but MCP annotations are per-tool, not per-argument. It
carries `destructiveHint=true` for the whole tool — the conservative direction, since
over-flagging a benign `power on` is safer than advertising a `power off` as harmless — with
the exception noted in its description. Splitting it per action was considered and rejected as
over-fragmenting the surface.

Because the destructive hint is a hand-set literal, it could drift from the policy.
Enforcement is not uniform: `force_crash` and `reprovision` run the three-check gate
(`assert_destructive_allowed`, `security/gate.py`; `control.py:186`, `systems.py:465`),
while `power` off/cycle/reset and `teardown` are admin-role-gated via `require_role`
(ADR-0037) and never call the gate; the gate reads a handler-built `DestructiveOp` plus the
allocation's `capability_scope`, **not** `OpContract`. So the guard anchors `destructiveHint`
to the **reviewed destructive set** above — every tool in it must carry the hint and no tool
outside it may — rather than to the gate call (which would leave `teardown` and `power`
unguarded). A backstop asserts every handler that invokes `assert_destructive_allowed` is in
the set, so a newly-gated op cannot be added without either carrying the hint or failing CI.
The backstop reaches only gate-callers, though: set *membership* for the admin-role-gated
ops (`teardown`, `power`) is a reviewed, un-backstopped judgement — adding a new admin-gated
destructive op needs a manual set update. An admin-role anchor can't substitute, because
`accounting.set_budget`/`set_quota` are `Role.ADMIN`-gated yet non-destructive.

## Component 2 — Generator

`scripts/gen_tool_reference.py`:

- Builds the app in-process via `build_app(pool, verifier=<stub>)`, injecting **both** a
  **null/stub pool** (`register()` only binds the pool reference and never touches it at
  registration time, `runs.py:625`) **and a stub verifier**. The verifier injection is not
  optional: `build_app` otherwise calls `build_verifier()`, which `_require_env`s
  `KDIVE_OIDC_{JWKS_URI,ISSUER,AUDIENCE}` (`mcp/auth.py:72`) and constructs a `JWTVerifier`
  the runtime may fetch a JWKS for — so a no-verifier build hard-fails in a bare CI checkout.
  With both injected the build needs no DB, no OIDC env, and no network. Then iterates the
  registry's `Tool` objects.
- Core is a **pure function** `registry → list[ToolDoc] → markdown`, unit-testable without
  the filesystem. The thin outer shell writes files.
- Per tool, renders: name, a maturity badge, hint badges (read-only / destructive — the
  two hints tools actually set; `idempotentHint` and a `long_running` marker are not set on
  any tool today, so they get no badge), the description, and a parameter table (name, type from the
  JSON schema, required, description).
- Groups tools by namespace; **deterministic** (sorted) ordering so output is stable and
  diffable.
- Emits `docs/guide/reference/<namespace>.md` plus a generated `reference/index.md` (a
  table of every tool → maturity). Each file opens with
  `<!-- generated by scripts/gen_tool_reference.py; do not edit -->` and the regen command.

## Component 3 — Conceptual layer (hand-authored)

`docs/guide/`, plain Markdown, each page citing the relevant ADR(s) so it stays anchored:

| Page | Covers | Cites |
|------|--------|-------|
| `index.md` | What KDIVE is, the build→boot→debug premise, how an agent drives it, a map into the reference | top-level-design |
| `concepts.md` | The six durable objects and their lifecycle ordering (`Resource ─< Allocation ─< System ─< Run ─< DebugSession`, plus `Investigation`) | ADR-0003, ADR-0026 |
| `response-envelope.md` | The uniform `ToolResponse` (id, status, `suggested_next_actions`, artifact `refs`, `error_category` on failure) and the references-not-log-dumps rule | ADR-0019 |
| `async-jobs.md` | The long-op pattern: a tool returns `{job_id, status: running}`; the agent polls `jobs.*`/`jobs.wait`. Which tools are long-running (a hand-authored table, since long-running is not a generated badge) | ADR-0008, ADR-0018 |
| `safety-and-rbac.md` | RBAC roles, the deny-by-default destructive-op gate (capability scope + role + profile opt-in), secret-by-reference + redaction | ADR-0020, ADR-0027, ADR-0028 |
| `errors.md` | The stable `ErrorCategory` taxonomy (`domain/errors.py`) and how to read/recover from a failure envelope | ADR-0019 |

The split holds the rot-prone surface (parameters, maturity) entirely in the generated
reference and the rarely-changing surface (concepts) in the hand-authored pages. Neither
duplicates the other: the concept pages never list parameters; the reference never
re-explains the envelope — it links to it.

## Component 4 — Guard

Two layers, because the docs job is optional but the test suite is not:

1. **`just docs` / `just docs-check`.** `docs` regenerates in place (mutating). `docs-check`
   regenerates to a temp dir and diffs against the committed `reference/`; non-zero on any
   difference. `docs-check` joins the `just ci` gate, so a parameter rename that is not
   reflected in the committed reference fails the **PR gate**, not an optional pipeline.
2. **`tests/mcp/test_tool_docs.py`.** Builds the registry via `build_app(pool,
   verifier=<stub>)` — the same null-pool + local-keypair-verifier path the service tests use,
   so it needs no OIDC env — and over that registry asserts: every tool has a non-empty
   description; every parameter has a description; every tool has a valid `meta.maturity`;
   `destructiveHint=true` holds for exactly the reviewed destructive set (`control.power`,
   `control.force_crash`, `systems.teardown`, `systems.reprovision`) and no tool outside it,
   with a backstop that every `assert_destructive_allowed` caller is in the set; and every
   `maturity=implemented` tool has the **tool-unique symbol its wrapper body names** referenced
   by at least one non-`live_vm`/`live_stack` test. The guard derives that symbol by parsing
   each wrapper body and frequency-ranking the **call-target** symbols (the callees — not
   parameter names, signature defaults, or literals) across all wrappers: the callee unique to
   one wrapper is the anchor (the per-tool handler for 42 tools; the distinct per-op builder
   `_set_breakpoint_op`, … for the 7 `debug.*` ops), while a shared callee like `run_engine_op`
   ranks non-unique and is excluded (else one test would cover all seven). Restricting to
   callees collapses each wrapper to exactly one anchor today; a wrapper that yields **zero or
   more than one** unique callee is a hard guard-test failure, not a silent skip — so a future
   fan-in or shape that defeats the anchor surfaces loudly. Anchored on the symbol,
   not the tool name; a coarse floor, so a stubbed tool can't be marked production-ready
   unchecked. This runs in the normal suite, so the invariants hold even when the docs job is
   skipped.

## Error handling

- The generator runs an **up-front completeness check** and fails fast, naming the offending
  tool (e.g. `accounting.report: parameter 'project' has no description`); it never emits
  blanks or papers over gaps. This matters because `just docs` is an authoring-time command —
  run after adding a tool, before the guard test — so it routinely meets incomplete metadata
  mid-backfill; an actionable per-tool error beats an opaque crash. The guard test
  (`tests/mcp/test_tool_docs.py`) is the enforcement of record; the generator's check is the
  fast local echo of the same invariant, not a second source of truth.
- `just docs-check` reports the offending file/diff on failure with the regen command, so
  the fix is "run `just docs` and commit".

## Testing

Behaviour, not implementation:

- **Generator:** feed a small fake registry → assert the rendered Markdown (badge rows,
  parameter table, namespace grouping, ordering). Assert the missing-description and
  missing-maturity inputs surface as a clear per-tool error (naming the tool/parameter),
  matching the generator's up-front completeness check.
- **Guard:** the invariants above, over the real registry. Verified to catch failure by
  dropping a description and confirming the test goes red, then restoring.

## Build sequence

Each step is independently verifiable; the guard lands before the generator so the
generator never runs against incomplete metadata.

1. **ADR-0047** — record the decision and the maturity criteria.
2. **Annotation helper** — `_docmeta.py`: constructors for the three annotation classes
   (read-only / destructive / state-mutating), so each class is spelled once rather than
   re-built at every registration.
3. **Backfill** — docstrings + `Field` descriptions + `meta.maturity` + annotations across
   all 49 `@app.tool` wrappers.
4. **Guard test** — `tests/mcp/test_tool_docs.py`; green once the backfill is complete.
5. **Generator** — `scripts/gen_tool_reference.py` + the committed `docs/guide/reference/`.
6. **Recipes + CI** — `just docs` / `just docs-check`; add `docs-check` to `just ci`.
7. **Conceptual layer** — the six hand-authored pages under `docs/guide/`.
