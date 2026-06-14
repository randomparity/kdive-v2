# Plan: Actionable error for unseeded kdump build-config (#373)

Design: `docs/adr/0105-build-config-seed-actionable-error.md`.

## Goal

When `runs.build` resolves the implicit kdump build-config and the catalog row is
missing, the `configuration_error` must carry a literal operator remediation command
in a structured field that reaches the operator (the job response `data`), not vague
prose. Keep the existing S3-tolerant `migrate` seed as the only seed path (part-2
decision: no second bare-migration seed).

## Files touched

- `src/kdive/build_configs/defaults.py` — add `SEED_REMEDIATION_COMMAND` constant;
  thread it into the missing-entry `CategorizedError` (`details["remediation"]` +
  message text).
- `tests/providers/test_build_common.py` — extend the existing
  `test_build_config_fetch_unknown_name_is_configuration_error` to assert the
  remediation detail; pin the affordance value to a literal so a migrate-command
  rename fails CI.
- `docs/adr/0105-…` and `docs/adr/README.md` — already committed.

No schema, migration, `__main__.py`, or seed-code change.

## Tasks (single session, TDD)

### Task 1 — failing test for the actionable affordance

In `tests/providers/test_build_common.py`:

1. Extend `test_build_config_fetch_unknown_name_is_configuration_error` (or add a
   sibling) to assert, on the raised `CategorizedError`:
   - `category is ErrorCategory.CONFIGURATION_ERROR` (unchanged)
   - `details["name"] == "nope"`
   - `details["remediation"] == "python -m kdive migrate"` (literal pin)
   - the message text names the migrate command.
2. Import and assert against `SEED_REMEDIATION_COMMAND` from
   `kdive.build_configs.defaults` so the pin is anchored to the one constant
   (a test that the constant equals the literal `"python -m kdive migrate"`,
   so a rename is a visible CI failure, not a silently-following test).
3. Run the focused test; confirm it fails for the expected reason (no `remediation`
   key / no constant yet).

Acceptance: the focused test fails before impl, passes after; the resolved path
(`test_build_config_fetch_returns_verified_bytes_and_closes_conn`) stays green
(the resolved path is unchanged — present row → bytes, no error).

### Task 2 — minimal implementation

In `src/kdive/build_configs/defaults.py`:

1. Add module constant `SEED_REMEDIATION_COMMAND = "python -m kdive migrate"` with a
   short docstring/comment tying it to ADR-0105 and the migrate seed step.
2. In `_fetch`, when `entry is None`, raise:
   ```
   CategorizedError(
       f"unknown build-config catalog entry; run `{SEED_REMEDIATION_COMMAND}` to seed it",
       category=ErrorCategory.CONFIGURATION_ERROR,
       details={"name": name, "remediation": SEED_REMEDIATION_COMMAND},
   )
   ```
3. Export `SEED_REMEDIATION_COMMAND` in `__all__`.

Acceptance: focused test green; `details["remediation"]` is the literal command;
message names it. `ty` and `ruff` clean.

### Task 3 — guardrails

`just lint`, `just type`, `just test` green. Then `just ci`. Commit.

## Conventions / guardrails

- Conventional Commits, ≤72-char imperative subject, `Co-Authored-By` trailer.
- Return the project's `CategorizedError` with the most specific category
  (`CONFIGURATION_ERROR`); affordance is a literal identifier in `details`, not prose.
- The remediation string carries no secret material — it passes the worker
  `_safe_detail`/redaction filter and `_safe_error_details` unchanged.

## Rollback / cleanup

Single-file source change plus a test assertion; revert is the two commits. No state,
schema, or external-service change to undo.

## Verification

- Unit: missing entry → `details["remediation"]` literal; constant pin test.
- The end-to-end worker→job-response surfacing is asserted structurally by the ADR
  (worker `_failure_context` already copies `details` → `failure_detail_*`); no new
  worker code, so no new worker test is required — the existing worker
  failure-context tests cover the copy mechanism generically.
