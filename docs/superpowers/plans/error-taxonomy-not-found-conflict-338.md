# Plan — `not_found` / `conflict` error categories (issue #338)

- **Spec:** [`../specs/2026-06-12-error-taxonomy-not-found-conflict.md`](../specs/2026-06-12-error-taxonomy-not-found-conflict.md)
- **ADR:** [0097](../../adr/0097-not-found-conflict-error-categories.md)
- **Branch:** `feat/error-taxonomy-not-found-conflict-338` (off `origin/main`)
- **Merge order:** #338 merges **first**; #339 rebases onto it.

Strict TDD: each step writes the failing test(s) first, runs the relevant test to confirm RED,
implements the minimum to go GREEN, then runs `just lint && just type && just test` before the
commit. One logical change per commit.

## Scope (exactly four modules + their tests)

In scope: `src/kdive/domain/errors.py`, `src/kdive/mcp/tools/_common.py`,
`src/kdive/mcp/tools/lifecycle/allocations.py`, `src/kdive/mcp/tools/_vmcore_targets.py`.
Touched-but-unchanged-behavior: `src/kdive/mcp/tools/ops/inventory.py` (no edit),
`src/kdive/mcp/tools/debug/introspect.py` (no edit — descoped). Docs: ADR-0097, spec, ADR README.

Out of scope (do NOT touch): `session_context.py`, `debug/ops.py`, `debug/sessions.py`, and any
sibling tool (accounting/buildconfig/shapes/artifacts/control), `cli/errors.py`.

## Step 1 — taxonomy enum (`domain/errors.py`)

**RED:** `tests/domain/test_errors.py` — add `test_not_found_value` / `test_conflict_value`
asserting `ErrorCategory.NOT_FOUND.value == "not_found"` and `ErrorCategory.CONFLICT.value ==
"conflict"`. Run `pytest tests/domain/test_errors.py` → fails (AttributeError).

**GREEN:** add `NOT_FOUND = "not_found"` and `CONFLICT = "conflict"` to `ErrorCategory`. Place
them in a small new "Object-lookup categories (#338)" block after the PoC block, before the
distributed block, so the diff is a clean insertion #339 can rebase around. Update the module
docstring's one-line taxonomy note to mention the two new lookup categories.

**Commit:** `feat(errors): add not_found and conflict error categories`

## Step 2 — `not_found` helper (`mcp/tools/_common.py`)

**RED:** `tests/mcp/core/` (the `_common` helpers test, or add `tests/mcp/test_common_helpers.py`
if none) — assert `not_found("abc").error_category == "not_found"`, `.status == "error"`,
`.object_id == "abc"`, and that `data` defaults to `{}`.

**GREEN:** add `def not_found(object_id, *, data=None) -> ToolResponse` returning
`ToolResponse.failure(object_id, ErrorCategory.NOT_FOUND, data=data or {})`, mirroring
`config_error`. Add `"not_found"` to `__all__`.

**Commit:** `feat(mcp): add not_found tool-response helper`

## Step 3 — allocations get/release/renew (`lifecycle/allocations.py`)

**RED:** in `tests/mcp/lifecycle/test_allocations_tools.py`:
- Update `test_get_other_project_allocation_is_not_found` to assert `error_category ==
  "not_found"` (it already has the right name).
- Add `test_get_absent_allocation_is_not_found` (valid random UUID, no row) → `not_found`.
- Add `test_get_ungranted_matches_absent_envelope` — request an allocation as project A, then
  `get` it as project B (ungranted) and `get` a random absent UUID as project B; assert the two
  failure envelopes are **identical** on `object_id`-shape contract: same `error_category` and
  same `data` (the no-leak assertion). (object_id differs by construction since it echoes the
  input id; assert `error_category` and `data` equal and both `status == "error"`.)
- Add `test_get_malformed_allocation_is_config_error` (`"nope"`) → `configuration_error`.
- **`release_allocation`** (`test_allocations_tools.py`): no existing release-of-absent test
  exists — ADD `test_release_absent_allocation_is_not_found` (random UUID → `not_found`) and
  `test_release_ungranted_allocation_is_not_found` (other project → `not_found`). Keep any
  malformed release assertion as `configuration_error`.
- **`renew_allocation`** (`test_allocations_renew.py`): **FLIP existing**
  `test_renew_unknown_allocation_is_config_error` (line 262, valid-but-absent UUID) to assert
  `not_found` (rename to `test_renew_unknown_allocation_is_not_found`). **PIN unchanged:**
  `test_renew_malformed_id_is_config_error` (272, malformed → `configuration_error`),
  `test_renew_terminal_allocation_is_stale_handle` (198), `test_renew_at_cap_is_config_error`
  (227), and `test_key_reused_across_request_kind_is_rejected` (296, PK-conflict fails *inside*
  renew, not the absent branch → stays `configuration_error`).

Run those test modules → the not_found assertions fail (still `configuration_error`).

**GREEN:** in `get_allocation`, `release_allocation`, `renew_allocation`, change **only** the
post-fetch branch `if alloc is None or alloc.project not in ctx.projects:` from
`return _config_error(allocation_id)` to `return _not_found(allocation_id)` (import the new
helper as `_not_found`). The pre-fetch `_as_uuid(...) is None` malformed guard stays
`_config_error`. Do not touch `request_allocation` or `_envelope_for_allocation`.

Update the three docstrings/the module docstring line that says "or a not-found-shaped result"
to name the `not_found` category now that it is real.

**Commit:** `fix(allocations): return not_found for absent/ungranted ids`

## Step 4 — from_vmcore resolver split (`mcp/tools/_vmcore_targets.py`)

**RED:** in `tests/mcp/test_vmcore_targets.py` (it exists):
- **PIN unchanged** `test_resolve_run_vmcore_target_rejects_bad_run_id` (line 75, the
  `uid is None` malformed branch → stays `CONFIGURATION_ERROR`). Must pass before AND after —
  this is the must-NOT-move guard.
- **FLIP existing** `test_resolve_run_vmcore_target_requires_recorded_build_id` (line 86, the
  `build_id is None` prerequisite-missing branch) to assert
  `category is ErrorCategory.NOT_FOUND` (rename to `..._missing_build_id_is_not_found`). The ADR
  classifies "no recorded build" as a missing target artifact → `not_found`.
- ADD `test_resolve_run_vmcore_target_absent_run_is_not_found` — a syntactically valid but
  absent UUID raises `CategorizedError` with `category is ErrorCategory.NOT_FOUND`. (Fails
  before the split.)

And in `tests/mcp/debug/test_introspect_tools.py` flip these four to `not_found`:
`test_from_vmcore_unbuilt_run_is_config_error` (null debuginfo),
`test_from_vmcore_no_build_step_is_config_error`,
`test_from_vmcore_no_captured_core_is_config_error`,
`test_from_vmcore_cross_project_is_config_error` (ungranted — the no-leak case). Rename each to
`..._is_not_found` and assert `error_category == "not_found"`. Leave
`test_from_vmcore_malformed_run_id_is_config_error` asserting `configuration_error`.

**GREEN:** in `resolve_run_vmcore_target`, keep `_target_config_error()` raised **only** for the
`uid is None` branch. Add `_target_not_found()` returning `CategorizedError(..., category=
ErrorCategory.NOT_FOUND)` and raise it from the other branches: `run is None or run.project not
in ctx.projects`, `run.debuginfo_ref is None`, `build_id is None`, `vmcore_ref is None`. The
`require_role` call is unchanged (a granted-but-unauthorized caller still raises
`AuthorizationError`, not a category — RBAC is orthogonal).

**Verification of the must-not-move guard:** confirm
`test_resolve_run_vmcore_target_rejects_bad_run_id` (malformed) is GREEN both before the split
(single helper) and after (still `configuration_error`). Confirm the renamed build-id test and
`..._absent_run_is_not_found` were RED before and GREEN after. This is the medium-finding pin.

**Commit:** `fix(introspect): from_vmcore returns not_found for absent run targets`

## Step 5 — regression guards for the unchanged seams

**RED/GREEN (assertions that must already hold post-Steps 1-4):**
- `inventory.list`: confirm the existing `test_malformed_resource_id_is_config_error` still
  asserts `configuration_error`, and the existing empty-filter test still asserts status `ok`.
  Add one explicit assertion if not already present that an absent `resource_id` is **not** a
  `not_found` failure. No code change.
- `introspect.run`: confirm `test_run_live_cross_project_is_config_error`,
  `test_run_live_non_live_session_is_config_error`, `test_run_live_non_drgn_live_session_is_
  config_error`, `test_run_live_unknown_helper_is_config_error`, and
  `test_run_live_malformed_session_id_is_config_error` are **unchanged** and pass — pinning the
  descope. No code change.
- CLI envelope: in `tests/cli/test_transport.py` (or test_tool_error_handling.py) add
  `test_not_found_envelope_maps_to_code_4` — `exit_code_for_envelope({"error_category":
  "not_found"}) == 4`. No code change (mapping already exists).

**Commit:** `test(errors): pin descope and not_found exit-code envelope mapping`

## Step 6 — generated docs

Run `just docs-check` and `just config-docs-check`. The enum is not enumerated in generated
references (verified: no `ErrorCategory`/category lists under `docs/guide/reference/`), so no
regeneration is expected. If either check reports drift, run `just docs` / `just config-docs`
and commit the regenerated file in its own commit. Otherwise no commit.

## Guardrails (before every commit)

`just lint && just type && just test`. Zero warnings. `just type` is whole-tree (src + tests).

## Branch review

`/challenge --json --base main` loop (≤5, stop on approve), then the `/security-review` skill
(this touches the error surface / no-leak boundary). Address every defensible finding, commit
each.

## Ship

Push; `gh pr create --base main` with a plain factual body ending `Closes #338`. Drive to
required-checks-green AND `mergeStateStatus == CLEAN` AND `mergeable == MERGEABLE`. Rebase onto
`origin/main` and rerun guardrails if BEHIND/DIRTY. Do **not** merge.

## #339 collision surface (for the handoff)

- `domain/errors.py`: the inserted `NOT_FOUND`/`CONFLICT` enum members (new lines in the enum
  body).
- `mcp/tools/_common.py`: the new `not_found` helper + `__all__` entry.
- `lifecycle/allocations.py`: the `_not_found` import and the three changed post-fetch branches
  in `get_allocation` / `release_allocation` / `renew_allocation`.
- `mcp/tools/_vmcore_targets.py`: the new `_target_not_found` helper + the changed raises in
  `resolve_run_vmcore_target` (propagates to both `introspect.from_vmcore` and
  `vmcore.postmortem_*`, which share the resolver).
- `mcp/tools/debug/introspect.py`: docstring-only change to `introspect_from_vmcore` (names the
  `not_found` category); `introspect.run` / `resolve_live_drgn_session` unchanged (descope).
- `db/schema/0026_not_found_conflict_categories.sql`: new forward-only migration widening the
  `runs.failure_category` / `jobs.error_category` CHECK constraints. **#339 must not reuse the
  0026 number** — if it adds another enum value it takes 0027+.
- `inventory.py`: **no change** (still imports `ErrorCategory`; #339 will edit it independently).

## Full flip-set of tests (for the #339 rebase and the no-surprise-reds guarantee)

**Existing tests that change verdict `configuration_error` → `not_found`:**
1. `tests/mcp/lifecycle/test_allocations_tools.py::test_get_other_project_allocation_is_not_found`
   (already correctly named).
2. `tests/mcp/lifecycle/test_allocations_renew.py::test_renew_unknown_allocation_is_config_error`
   → rename `..._is_not_found`.
3. `tests/mcp/test_vmcore_targets.py::test_resolve_run_vmcore_target_requires_recorded_build_id`
   → rename `..._missing_build_id_is_not_found`.
4-7. `tests/mcp/debug/test_introspect_tools.py`:
   `test_from_vmcore_unbuilt_run_is_config_error`, `test_from_vmcore_no_build_step_is_config_error`,
   `test_from_vmcore_no_captured_core_is_config_error`, `test_from_vmcore_cross_project_is_config_error`
   → each rename `..._is_not_found`.
8-10. `tests/mcp/lifecycle/test_vmcore_tools.py` (the `vmcore.postmortem_crash` /
   `postmortem_triage` tools share `resolve_run_vmcore_target`, so they propagate the same flip —
   discovered by the full-suite checkpoint, not in the original plan):
   `test_postmortem_crash_unbuilt_run_is_config_error` → `..._is_not_found`,
   `test_postmortem_triage_propagates_failure` → `test_postmortem_triage_propagates_not_found`,
   `test_postmortem_crash_no_core_is_config_error` → `..._is_not_found`. The
   `test_postmortem_crash_provenance_mismatch_is_config_error` test stays `configuration_error`
   (the *provider* raises that, not the resolver), as does a malformed-command-batch postmortem.
11-12. `tests/db/test_migrate.py` — the migration-version list (two hardcoded lists) gains
   `0026`, and `test_check_constraint_covers_every_enum_value` now passes because migration 0026
   widens the `runs.failure_category` / `jobs.error_category` CHECK constraints.

**New tests added:** get-absent, get-ungranted-matches-absent (no-leak), get-malformed-pin,
release-absent, release-ungranted, renew already covered by flip, resolver absent-run,
errors enum values, `_common.not_found` helper, not_found→exit-4 envelope.

**Pinned must-NOT-move (stay `configuration_error`):** every malformed-id test
(`test_*_malformed_id_is_config_error`, `test_resolve_run_vmcore_target_rejects_bad_run_id`,
`test_malformed_resource_id_is_config_error`); all `request_allocation` admission/selector
tests (cordoned/degraded/offline host, absent-card); `test_renew_at_cap_is_config_error`,
`test_key_reused_across_request_kind_is_rejected`; all `introspect.run` tests
(`test_run_live_*`, the descope); `test_renew_terminal_allocation_is_stale_handle` and
`test_release_terminal_allocation_is_stale_handle` (stale_handle, untouched).
