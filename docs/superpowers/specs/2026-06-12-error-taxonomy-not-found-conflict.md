# Spec — `not_found` / `conflict` error categories (issue #338, finding S1)

- **Date:** 2026-06-12
- **ADR:** [0097](../../adr/0097-not-found-conflict-error-categories.md)
- **Issue:** #338
- **Status:** Draft

## Problem

`ErrorCategory` (`src/kdive/domain/errors.py`) has no `not_found` or `conflict`. The CLI
(`src/kdive/cli/errors.py`, ADR-0089) reserves stable exit codes `4 = not_found` and
`5 = conflict`, but no server tool emits those categories, so exit 4 and 5 are unreachable.

A well-formed-but-absent object id is reported as `configuration_error` (exit 2) — the same
category used for a *malformed* id. "You typed garbage" and "that id doesn't exist" collapse
to one category and one exit code. This was found while exercising the full `kdivectl` verb
matrix against the live demo cluster (finding S1).

## Goal

A caller can distinguish, by `error_category` (and the CLI exit code derived from it):

| Cause | category | exit |
|-------|----------|------|
| Malformed id (UUID parse / payload validation fails) | `configuration_error` | 2 |
| Valid id, no visible row (absent **or** in an ungranted project) | `not_found` | 4 |
| Uniqueness/state conflict | `conflict` | 5 (reserved; no producer yet) |

## Invariants (must hold)

1. **No-leak (load-bearing, ADR-0020).** An id that resolves to a row in an *ungranted*
   project MUST return the *same* category and an *identical* envelope shape as a genuinely
   absent id. The fix moves **both** cases together from `configuration_error` to `not_found`.
   No branch may map ungranted → `authorization_denied` or otherwise differ from absent —
   that would be a membership oracle.
2. **Malformed stays `configuration_error`.** The split is by cause (parse failure vs.
   absent row), not by tool.
3. **Stable categories untouched.** `transport_conflict`, `stale_handle`,
   `infrastructure_failure`, `authorization_denied` keep their existing producers and wire
   strings. We do not rename any of them to `conflict`.
4. **`error_category`-iff-failure (ADR-0019)** is preserved — `not_found` is a failure
   category carried on status `"error"`.

## Changes

### 1. Taxonomy — `src/kdive/domain/errors.py`

Add to `ErrorCategory`:

- `NOT_FOUND = "not_found"`
- `CONFLICT = "conflict"`

`CONFLICT` is defined but has no producer in this change (ADR-0097, "Conflict stays
defined-but-unemitted").

### 2. A shared `not_found` helper — `src/kdive/mcp/tools/_common.py`

Add a `not_found(object_id, *, data=None)` helper mirroring the existing `config_error`
helper, so every call site uses one spelling and the no-leak envelope shape is centralized.

### 3. `allocations.*` — `src/kdive/mcp/tools/lifecycle/allocations.py`

In `get_allocation`, `release_allocation`, `renew_allocation`: the malformed-id guard
(`_as_uuid(...) is None`) stays `config_error`. The
`alloc is None or alloc.project not in ctx.projects` branch returns `not_found` instead of
`config_error`. (Three call sites; same edit.)

`request_allocation` keeps `configuration_error` for a malformed selector (parse failure, not
a lookup). The terminal-`failed` render stays `infrastructure_failure`.

### 4. `introspect.from_vmcore` — `_vmcore_targets.py` (in scope)

`resolve_run_vmcore_target`: keep `_target_config_error` (malformed `run_id`, the
`_as_uuid is None` branch) **and** introduce a *second, distinct* `_target_not_found` helper
for every "row absent / ungranted / prerequisite artifact missing" branch (run absent or
ungranted, null `debuginfo_ref`, no build step, no captured core). The two helpers stay
separate so the malformed branch cannot silently drift into `not_found`.

### 4b. `introspect.run` session lookups — `introspect.py` (descoped, stays `configuration_error`)

`resolve_live_drgn_session` resolves through the **shared** `resolve_debug_session_context`
helper (`mcp/tools/debug/session_context.py`), also consumed by `debug/ops.py`,
`debug/sessions.py`, and the connect/control plane. That helper maps *all* failure modes
(bad id, unknown/ungranted session, not-live, wrong-transport) to `configuration_error` behind
a single `debug_session_error`. Re-mapping only the "unknown session" code to `not_found` would
require either editing that shared helper (re-categorizing failures for tools outside #338's
scope) or having `resolve_live_drgn_session` reach into the returned envelope's `data["code"]`
and rewrite it (coupling to a sibling tool's internal discriminator). Both are out of scope.

Therefore `introspect.run`'s absent/ungranted session **stays `configuration_error`** for now
(`_session_config_error` unchanged), and its unknown-`helper` guard stays `configuration_error`
(a bad argument, not a missing object). Lifting the session lookup to `not_found` is deferred to
a change that owns the shared debug-session resolver (ADR-0097 Decision §3).

### 4c. Sibling tools that emit `configuration_error` on a valid-but-absent id stay unchanged

Issue #338 names exactly `domain/errors.py`, `mcp/tools/ops/inventory.py`,
`mcp/tools/debug/introspect.py`, and `allocations.py`. Other tools also report a
valid-but-absent id as `configuration_error` — e.g. `accounting.usage_by_investigation`,
`buildconfig.get`, `shapes.delete`, `artifacts.search`, control/`power` cross-project. Those are
**out of scope**; migrating the whole surface is a separate, larger change. This PR moves only
the issue-named seams.

### 5. `inventory.list` — unchanged at runtime

A malformed `resource_id` stays `configuration_error`. An absent `resource_id` yields an empty
collection (status `ok`); this is a filtered audit list, not a by-id lookup, and must not
become `not_found`. No code change beyond keeping the existing import.

### 6. CLI — no change

`src/kdive/cli/errors.py` already maps `not_found → 4`, `conflict → 5`, with passing tests in
`tests/cli/test_transport.py`.

## Tests (TDD — write failing first)

- `errors`: `NOT_FOUND.value == "not_found"`, `CONFLICT.value == "conflict"`.
- `allocations.get`: valid-but-absent uuid → `not_found`; **ungranted-project row →
  `not_found` and envelope identical to the absent case** (same `object_id`, same
  `error_category`, same `data`); malformed id → `configuration_error`. Update the misnamed
  existing `test_get_other_project_allocation_is_not_found` to assert `not_found`.
- `allocations.release` / `renew`: absent and ungranted → `not_found`; malformed →
  `configuration_error`.
- `introspect.from_vmcore`: absent/ungranted run → `not_found`; null `debuginfo_ref` / no
  build / no core → `not_found`; malformed run_id → `configuration_error`.
- **Resolver-level (must-not-move guard):** `resolve_run_vmcore_target` with a malformed
  `run_id` raises `CategorizedError(CONFIGURATION_ERROR)`; with a syntactically valid but
  absent `run_id` raises `CategorizedError(NOT_FOUND)`. This pins the two helpers apart at the
  function boundary, independent of the tool wrapper.
- `introspect.run` (descoped): absent/ungranted session **stays `configuration_error`**;
  wrong-state / wrong-transport stays `configuration_error`; unknown helper stays
  `configuration_error`. The existing assertions are unchanged — this confirms the descope.
- `inventory.list`: absent `resource_id` filter → empty collection, status `ok` (regression
  guard that we did NOT turn it into `not_found`); malformed → `configuration_error`.
- End-to-end exit code: a `not_found` envelope → `exit_code_for_envelope` returns 4 (already
  covered by `exit_code_for_category`; add one envelope-level assertion).

## Compatibility & sequencing (vs. existing tests / runbooks and issue #339)

- **Tests that change verdict (`configuration_error` → `not_found`):** seven existing tests on
  in-scope seams flip and must be updated:
  1. `test_allocations_tools.py::test_get_other_project_allocation_is_not_found` (already named
     for the new value).
  2. `test_allocations_renew.py::test_renew_unknown_allocation_is_config_error` (valid-but-absent
     id) → rename `..._is_not_found`.
  3. `test_vmcore_targets.py::test_resolve_run_vmcore_target_requires_recorded_build_id`
     (prerequisite-missing branch) → rename `..._missing_build_id_is_not_found`.
  4-7. `test_introspect_tools.py` `from_vmcore` `unbuilt_run` / `no_build_step` /
     `no_captured_core` / `cross_project`.
  8-10. `test_vmcore_tools.py` `postmortem_crash`/`postmortem_triage` for unbuilt-run /
     no-core — the `vmcore.*` postmortem tools share `resolve_run_vmcore_target`, so the flip
     propagates there too (caught by the full-suite checkpoint; the original "exactly seven"
     count was low). `postmortem_crash_provenance_mismatch` stays `configuration_error` (raised
     by the provider, not the resolver).
  Plus `test_migrate.py` gains migration `0026` in its version lists and now passes the
  SQL↔enum constraint coverage test because 0026 widens both CHECK constraints.

  All malformed-id tests stay `configuration_error` (the must-not-move pins:
  `test_renew_malformed_id_is_config_error`, `test_resolve_run_vmcore_target_rejects_bad_run_id`,
  `test_from_vmcore_malformed_run_id_is_config_error`, `test_malformed_resource_id_is_config_error`),
  and every `introspect.run` (`test_run_live_*`) test is **left unchanged** (descope). The full
  flip-set + pin-set lives in the plan.
- **Out-of-scope tests stay green untouched:** sibling tools (`accounting.usage_by_investigation`,
  `buildconfig.get`, `shapes.delete`, `artifacts.search`, control `power` cross-project, and
  `introspect.run`) keep asserting `configuration_error`; this PR does not touch them.
- **Runbooks:** `docs/runbooks/four-method-live-run.md` and `remote-live-stack.md` mention
  `configuration_error` only for provider-config-absent / malformed-input cases, not for a
  valid-but-absent object id; no runbook branches on exit-2-as-not-found. No runbook edit
  needed.
- **Merge order vs. #339:** **#338 merges first.** #339 (authz-denial enveloping) also touches
  `errors.py`, `inventory.py`, and `allocations.py`; it is expected to rebase onto this change.
  The collision surface is named in the handoff. This PR does not pre-empt #339's authz work and
  keeps `authorization_denied` exactly as-is (the no-leak rule means an ungranted lookup is
  `not_found`, never `authorization_denied`, so the two changes are semantically disjoint on the
  read path).

## Out of scope

- Emitting `conflict` from any tool (no current state-conflict seam needs it; ADR-0097).
- Lifting `introspect.run` / the shared `resolve_debug_session_context` to `not_found`
  (deferred; would re-categorize sibling debug/connect/control tools — ADR-0097 §3).
- Migrating sibling tools (accounting/buildconfig/shapes/artifacts/control) to `not_found`.

## Disposition of /challenge findings (round 1)

- **High (introspect.run shared-resolver split):** ACCEPTED. Took the safe descope —
  `introspect.run` session lookups stay `configuration_error`; `not_found` is limited to
  `from_vmcore`'s `resolve_run_vmcore_target` and the allocations branches. The shared
  `debug_session_error` / `resolve_debug_session_context` helper is **not** edited (Change 4b,
  ADR-0097 §3).
- **Medium (two helpers + pin must-not-move):** ACCEPTED. Kept `_target_config_error` and a
  new `_target_not_found` distinct; added a resolver-level test pinning malformed→config_error
  and absent→not_found (Tests, Change 4).
- **Low (compat/sequencing):** ACCEPTED. Added the Compatibility & sequencing section above;
  enumerated the two flipping tests, confirmed no runbook branches on exit-2-as-not-found, and
  fixed the merge order as #338-first.
