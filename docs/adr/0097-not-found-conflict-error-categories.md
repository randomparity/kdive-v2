# ADR 0097 — `not_found` / `conflict` error categories for object lookups (S1)

- **Status:** Proposed
- **Date:** 2026-06-12
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0001](0001-greenfield-rewrite.md) (the stable
  `ErrorCategory` taxonomy this extends), [ADR-0019](0019-tool-response-envelope.md) (the
  uniform `ToolResponse` envelope and the `error_category`-iff-failure invariant),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (RBAC reads gate on project membership;
  an ungranted row is rendered as absent), [ADR-0089](0089-operator-cli-mcp-client.md) (the
  CLI exit-code contract that already reserves `4 = not_found` and `5 = conflict`).
- **Spec:** [`../superpowers/specs/2026-06-12-error-taxonomy-not-found-conflict.md`](../superpowers/specs/2026-06-12-error-taxonomy-not-found-conflict.md)
- **Issue:** #338 (finding S1)

## Context

`ErrorCategory` (`src/kdive/domain/errors.py`) is the closed, stable set of failure strings a
tool may report; handlers pick the most specific value and never invent strings (ADR-0001,
the cross-cutting "stable error taxonomy" invariant). The CLI (`src/kdive/cli/errors.py`,
ADR-0089) maps each category to a fixed nonzero exit code so scripts and CI can branch on the
*kind* of failure, not just success/failure.

Exercising the full `kdivectl` verb matrix against the live demo cluster surfaced finding S1:

- `ErrorCategory` defines neither `not_found` nor `conflict`.
- The CLI exit table already reserves `4 = not_found` and `5 = conflict` (with passing
  unit tests in `tests/cli/test_transport.py`), but because no server tool emits those
  categories, **exit codes 4 and 5 are currently unreachable** — a documented contract with
  no producer.
- A well-formed-but-absent object id is reported as `configuration_error` (exit 2), the same
  category and exit code used for a *malformed* id. "You typed garbage" and "that id doesn't
  exist" are indistinguishable to a caller.

Concretely, three lookup seams collapse the two cases:

- `allocations.get` / `release` / `renew` — a non-UUID id is `configuration_error` (correct),
  but a syntactically valid id with no matching row is *also* `configuration_error`
  (`src/kdive/mcp/tools/lifecycle/allocations.py`, via `_config_error`).
- `inventory.list` — a non-UUID `resource_id` is `configuration_error` (correct); an absent
  resource simply returns an empty collection (a filter, not a lookup — see Decision).
- `introspect.from_vmcore` / `introspect.run` — a malformed id and an absent
  Run/DebugSession both raise a `CONFIGURATION_ERROR`-categorized `CategorizedError`
  (`_vmcore_targets.py`, `introspect.py`).

### The no-leak invariant (load-bearing)

Project-scoped reads deliberately render a row in an *ungranted* project as if it did not
exist, so a caller cannot probe project membership by id (ADR-0020). Today the absent case
and the ungranted case both map to `configuration_error`, so they are indistinguishable. Any
change here MUST keep that property: whatever category an absent-but-valid id takes, an
**ungranted-project** id MUST take the *same* category, with an identical envelope shape (same
`object_id`, same `error_category`, same absence of project-revealing `data`). Splitting them
— e.g. `not_found` for absent but `authorization_denied` for ungranted — would reintroduce
the membership oracle. See `src/kdive/mcp/tools/lifecycle/allocations.py` ("A row in an
ungranted project is indistinguishable from not-found").

## Decision

1. **Extend the taxonomy.** Add two values to `ErrorCategory`:
   - `NOT_FOUND = "not_found"` — a syntactically valid object id that resolves to no row the
     caller may see.
   - `CONFLICT = "conflict"` — a uniqueness or state conflict (reserved; see "Conflict stays
     defined-but-unemitted" below).

2. **Object-lookup tools return `not_found` for absent-but-well-formed ids.** Parse failures
   stay `configuration_error`. The split is by *cause*, not by *tool*:
   - **Malformed id** (fails UUID parse / payload validation) → `configuration_error` (exit 2).
     Unchanged.
   - **Valid id, no visible row** (absent, or in an ungranted project) → `not_found` (exit 4).

   Affected seams:
   - `allocations.get` / `release` / `renew`: the `alloc is None or alloc.project not in
     ctx.projects` branch returns `not_found` instead of `config_error`. The malformed-id
     guard above it keeps returning `config_error`.
   - `_vmcore_targets.resolve_run_vmcore_target` (the `introspect.from_vmcore` lookup): the
     malformed-`run_id` guard keeps a `configuration_error` helper; every "row absent /
     ungranted / prerequisite artifact missing" branch raises `not_found` via a *second,
     distinct* helper. The two helpers stay separate precisely so the malformed branch
     cannot drift into `not_found`.
   - `inventory.list` keeps `configuration_error` for a malformed `resource_id`. It is a
     **filtered cross-project audit list**, not a by-id lookup: an absent `resource_id`
     yields an empty collection (status `ok`), not a failure. We do not turn an empty filter
     into `not_found` — that would conflate "no rows matched your filter" with "the object
     you named is gone", and would change a successful read into a failure. (The enum value
     is still imported there for the malformed branch; no behavior change.)

3. **`introspect.from_vmcore` prerequisite-missing cases become `not_found`; `introspect.run`
   session lookups stay `configuration_error` (scoped descope).** For `from_vmcore` (resolved
   by `resolve_run_vmcore_target`), a Run that exists but has a null `debuginfo_ref`, no
   recorded `build` step, or a System with no captured core is "the *core* you asked to
   introspect does not exist" — a not-found of the target artifact, not malformed input. These
   move to `not_found`; the malformed-`run_id` guard stays `configuration_error`.

   `introspect.run`'s live-session lookup resolves through the **shared**
   `resolve_debug_session_context` helper (`mcp/tools/debug/session_context.py`), which is
   also consumed by `debug/ops.py`, `debug/sessions.py`, and the connect/control plane. That
   helper categorizes *every* failure mode (bad id, unknown/ungranted session, not-live,
   wrong-transport) as `configuration_error` behind a single `debug_session_error`. Re-mapping
   only its "unknown session" code to `not_found` would mean either editing that shared helper
   (re-categorizing failures for tools outside #338's scope — a contract change we are
   unwilling to make blind) or having `resolve_live_drgn_session` reach into the returned
   envelope's `data["code"]` and rewrite it (coupling `introspect.run` to a sibling tool's
   internal discriminator). Both are out of scope for this taxonomy fix. Therefore
   `introspect.run`'s absent/ungranted session **stays `configuration_error`** for now; lifting
   it to `not_found` is deferred to a change that owns the shared debug-session resolver. This
   is a deliberate, narrow under-fix recorded honestly rather than a silent gap.

4. **No CLI change.** `src/kdive/cli/errors.py` already maps `not_found → 4` and
   `conflict → 5`. Adding the producers makes exit 4 reachable end-to-end; no edit to the CLI
   or its tests is required.

### Conflict stays defined-but-unemitted (stated honestly)

No tool in the current surface emits `conflict`. The closest existing semantics —
uniqueness/single-attach collisions — already have dedicated, stable categories that we are
**not** renaming: a second gdbstub attach is `transport_conflict`; a System that already
carries a non-terminal Run is `transport_conflict` (ADR-0032, ADR-0067); a terminal
allocation re-driven is `stale_handle`. Re-pointing any of those at a generic `conflict` would
be a gratuitous wire-string change to a stable contract for no caller benefit.

Therefore `CONFLICT` is added to the enum (so the value exists, the CLI mapping has a named
producer-side counterpart, and a future state-conflict seam can adopt it without another
taxonomy ADR), but **exit code 5 remains defined-but-unemitted** until a concrete
state-conflict seam needs it. We record this explicitly rather than inventing a synthetic
conflict path to "light up" the code. Issue #339 (authz-denial enveloping) is the next change
to this surface and may revisit whether any denial is better modeled as a conflict; this ADR
does not pre-decide that.

## Consequences

- Exit code 4 (`not_found`) becomes reachable; a script can distinguish "bad id" (2) from
  "gone" (4). Exit 5 stays reserved.
- **Behavior change for existing callers:** absent/ungranted `allocations.*` ids and absent
  introspect targets now return `error_category: "not_found"` (exit 4) where they previously
  returned `configuration_error` (exit 2). This is the intended correction. The
  ungranted-project envelope stays byte-identical to the genuinely-absent envelope, so the
  no-leak property is preserved — the change moves *both* cases together from
  `configuration_error` to `not_found`.
- One existing test (`test_get_other_project_allocation_is_not_found`) asserted the old
  `configuration_error` value despite its name; it is corrected to assert `not_found`, which
  matches both its name and the no-leak intent.
- `inventory.list` is unchanged at runtime.
- Future state-conflict producers adopt `CONFLICT` directly; the stable
  `transport_conflict` / `stale_handle` categories are untouched.

## Alternatives considered

- **Map ungranted → `authorization_denied`, absent → `not_found`.** Rejected: reintroduces
  the membership oracle the no-leak rule exists to prevent (ADR-0020). The two cases must stay
  indistinguishable.
- **Treat an absent `inventory.list` filter as `not_found`.** Rejected: it is a filtered list,
  not a by-id fetch; an empty result is a successful read, and failing it would break the
  audit-sweep use case ("confirm a host is drained" legitimately returns zero rows).
- **Synthesize a `conflict` producer to make exit 5 reachable.** Rejected: inventing a code
  path to satisfy a reserved exit code is a phantom feature. We document the gap instead.
