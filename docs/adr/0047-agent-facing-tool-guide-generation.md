# ADR 0047 — Agent-facing tool guide generated from the registry

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-05
- **Deciders:** David Christensen
- **Spec:** [`../superpowers/specs/2026-06-05-agent-facing-tool-guide-design.md`](../superpowers/specs/2026-06-05-agent-facing-tool-guide-design.md)
- **Builds on:** [ADR-0010](0010-fastmcp-framework-auth.md) (FastMCP is the tool framework),
  [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse` envelope the reference
  documents), [ADR-0020](0020-rbac-audit-gate-implementation.md)/[ADR-0028](0028-control-plane-power-force-crash.md)
  (the destructive-op gate whose trigger the destructive annotation is cross-checked against).

## Context

KDIVE documents the system for the people who build it (ADRs, specs, plans, runbook) but
not for the parties that consume the MCP service: the coding agent driving the tools and
the developer pointing it at KDIVE. The registered tool surface (49 tools across 13
namespaces today, and growing) has no reference for any of them; a consumer must read
source.

Two forces shape the answer. First, the surface moves, so a hand-authored reference drifts
and nothing catches it. Second, the surface is not uniformly real — the
allocation/accounting/state-machine spine is implemented and unit-tested, while the
build → boot → crash → introspect path leans on stubbed provider seams
(`build-guest-image.sh`, a placeholder image digest) verified only under the gated
`live_vm`/`live_stack` markers. A reference that lists every registered tool uniformly
would present stubbed tools as production-ready.

FastMCP 3.4.0 exposes `description`, `tags`, `meta`, and standard MCP `annotations` on every
registered `Tool`, all readable off the registry — the same metadata an agent sees in
`tools/list`. That makes a generated, code-derived reference feasible without a parallel
metadata store.

## Decision

We will publish a **layered agent-facing guide** under `docs/guide/`: a hand-authored
conceptual layer (concepts, response envelope, async-jobs, RBAC/safety, errors) and a
per-namespace tool **reference generated from the live FastMCP registry** by
`scripts/gen_tool_reference.py`.

To make generation honest and drift-proof:

- Every `@app.tool` wrapper carries a **docstring** (tool description), an
  `Annotated[..., Field(description=...)]` on **every parameter**, a
  **`meta={"maturity": ...}`** marker (`implemented` | `partial` | `planned`), and standard
  MCP **`annotations`** set explicitly on the wrapper as a reviewed claim.
- The `annotations` are **not** derived from `OpContract`. ADR-0063 makes typed
  `ProviderRuntime` ports the production M0/M1 seam, so no capability `BoundOp` exists at
  registration time or for the registry-reading generator. Tools span
  three classes the annotation must distinguish: **read-only** queries (`*.get`/`*.list`,
  `accounting.usage`, `jobs.get`) → `readOnlyHint`; **destructive-administration** ops
  (`control.power` off/cycle/reset, `control.force_crash`, `systems.teardown`,
  `systems.reprovision` — ADR-0028/ADR-0037) → `destructiveHint`; and **state-mutating,
  non-destructive** tools (`allocations.release`/`renew`, `accounting.set_budget`/`set_quota`,
  `investigations.*` writes, `jobs.cancel`) → `readOnlyHint=false` with no destructive
  claim. A blanket "non-query ⇒ read-only" rule would mislabel that third class, so the
  classification is explicit per tool. `control.power` needs a call-out: it is one tool
  spanning a reversible `power on` (`operator`) and the destructive `off`/`cycle`/`reset`
  (`admin`), but MCP annotations are per-tool, not per-argument. It carries
  `destructiveHint=true` for the whole tool — the conservative direction, since over-flagging
  a benign `power on` is safer than advertising a `power off` as harmless — with the
  reversible exception stated in its description. Splitting it into per-action tools was
  considered and rejected as over-fragmenting the surface for a single tool.
- A **guard** fails CI on any drift between the committed reference and a fresh generation,
  and a pytest test fails on any tool missing a description, a parameter description, or a
  valid maturity value. Two further invariants the same test enforces over the live registry:
  - **The destructive hint matches a reviewed set.** `destructiveHint=true` must hold for
    exactly the declared destructive-administration set — `control.power`,
    `control.force_crash`, `systems.teardown`, `systems.reprovision` (ADR-0028/ADR-0037) —
    and for no tool outside it. Enforcement is uneven (`force_crash`/`reprovision` run the
    three-check gate `assert_destructive_allowed`; `power` off/cycle/reset and `teardown` are
    admin-role-gated via `require_role`), so the hint is anchored to the *set*, not to the
    gate call. A backstop asserts every handler that invokes `assert_destructive_allowed` is
    in the set, so a newly-gated op cannot be added without either carrying the hint or
    failing CI. This deliberately covers `teardown` — the most irreversible op — which a
    gate-call-only check would leave unguarded. The backstop reaches only the gate-callers,
    though: the admin-role-gated members (`teardown`, `power`) stay in the set by review
    alone, with no automated completeness check, so adding a new admin-gated destructive op
    needs a manual set update. An admin-role anchor cannot substitute — `accounting.set_budget`
    /`set_quota` are `Role.ADMIN`-gated yet correctly non-destructive, so keying off the role
    would false-positive.
  - **`implemented` has a falsifiable floor.** A tool marked `maturity=implemented` must have
    the **tool-unique symbol its wrapper body names** referenced by at least one
    non-`live_vm`/`live_stack` test. For 42 of the 49 tools that symbol is the async handler
    the wrapper delegates to 1:1 (`create_run`, `teardown_system`). The 7 `debug.*` engine ops
    are the exception: they share one dispatcher (`run_engine_op`), so the floor anchors on the
    distinct per-op builder each wrapper names (`_set_breakpoint_op`, `_read_memory_op`, …),
    never on the shared `run_engine_op` — which would let one test satisfy the floor for all
    seven. The anchor is a symbol, not the tool name (tools are tested through these symbols,
    so a name-keyed check would misfire). This is a concrete symbol-reference check (not the
    fuzzy name→tool inference rejected below) and a coarse floor — a reference is not proof of
    a meaningful assertion — but it stops a tool from being marked production-ready while no
    stock-host test touches its code.

  `docs-check` joins the `just ci` PR gate.

**Maturity criteria** (the reviewed claim each marker asserts):

- `implemented` — backed by a real provider op or core DB operation, covered by non-live
  tests, works on a stock host.
- `partial` — wired end-to-end but depends on a stubbed seam, or verified only under
  `live_vm`/`live_stack`.
- `planned` — registered surface whose backing op is a stub or not yet functional.

## Consequences

- The reference cannot describe a non-existent tool or omit a real one, and cannot present
  a stubbed tool as production-ready without a reviewer setting `maturity` to say so.
- The description backfill upgrades the **live** `tools/list` schema, so real agents get
  better tool/parameter descriptions and standard hints — a runtime benefit, not only a
  doc.
- New obligation: adding a tool now requires a docstring, parameter descriptions, a
  maturity marker, and explicit MCP annotations, or CI fails — and `destructiveHint` must
  match the reviewed destructive-administration set (with a backstop that a gate-guarded
  handler cannot be omitted from it). This is the intended cost — it keeps the surface
  self-documenting and the destructive hint consistent with the reviewed set. Set
  *membership* for the admin-gated ops (`teardown`, `power`) is a reviewed, un-backstopped
  judgement, the same accepted limit as the maturity marker below.
- New obligation: a parameter or tool change requires regenerating and committing the
  reference (`just docs`), enforced by `just docs-check` in the PR gate.
- The maturity marker is a human judgement; an out-of-date marker is possible. It is
  co-located with the tool and bounded by the criteria above, reviewed like any other code
  change, which is the accepted limit of a marker-based signal.

## Alternatives considered

- **Hand-authored reference.** Rejected: the tool surface across a moving codebase drifts silently;
  no guard short of manual review, which is exactly what failed to exist.
- **Hand-authored + name-only drift test.** Rejected: catches added/removed tools but not
  parameter or description drift, and does nothing for the live schema.
- **Maturity inferred from test coverage.** Rejected *as the source of truth*: inferring a
  marker *value* from coverage is too fuzzy, and a reviewed explicit marker states intent
  precisely. Coverage is still used as a one-directional *floor* — an `implemented` tool's
  tool-unique symbol must be referenced by a non-live test. That floor is a concrete
  symbol-reference check (parsed from the wrapper body — the per-tool handler, or the per-op
  builder for the shared-dispatcher `debug.*` ops; the registry exposes the wrapper, not the
  handler), not the fuzzy name→tool inference, so the marker stays falsifiable without
  pretending coverage equals maturity.
- **Sidecar metadata module** (descriptions in a separate table). Rejected: a non-idiomatic
  indirection FastMCP does not read, so it would not improve the live schema and would
  itself drift from the registrations.
- **Published doc site (mkdocs/Sphinx).** Deferred, not rejected: adds a dependency and
  build infra the repo lacks today, against the YAGNI/justify-dependencies rule. It layers
  cleanly on the Markdown later when a reader needs a hosted site.
