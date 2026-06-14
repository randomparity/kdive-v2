# Red-team finding — CI never type-checked the test tree

**Date:** 2026-06-04 · **Pass 7 incidental, now fixed.** **Scope:** the verification
harness (`.github/workflows/ci.yml`, `justfile`). Surfaced while fixing a `main` test-tree
`ty` failure (`_FakeProvisioner` missing `reprovision`, added to the `Provisioner` protocol
by PR #75) that blocked the local prek hook but had merged green.

## The gap (guardrail hole)

`ty` is the project's hard type gate, but **no CI step type-checked `tests/`**:

* the **lint · type · test** job ran `uv run ty check src` — `src/` only;
* the **pre-commit hooks** job ran `prek run --all-files` with `env: SKIP: ty` — the
  whole-tree `ty` hook (`pass_filenames: false`) was deliberately skipped to keep that job
  venv-free, justified by "the lint-type-test job already covers ty."

That justification was false: the lint-type-test job covered `ty` for `src/` only. The test
tree was type-checked **only** by the *local* prek hook, which CI skips — so a type error
confined to `tests/` merged to `main` green. (`ruff` already ran on `.` in CI, so lint was
whole-tree while type-check was not — an inconsistency.)

## Differential proof

On clean `main`, with a deliberate `tests/` type error injected:

| Command | Result |
|---|---|
| `uv run ty check` (no error) | All checks passed — the fix won't red CI |
| `uv run ty check src` (CI's *old* step, error present) | **All checks passed** — the gap |
| `uv run ty check` (proposed step, error present) | **Found 1 diagnostic** — gap closed |

## Fix — one definition, three callers delegate

The deeper cause was duplication: the lint/type/test commands were written inline in
`ci.yml`, again as `justfile` recipes, and again in the pre-commit `ty` hook — so the CI
copy could drift to `src`-only while the others stayed whole-tree. The fix makes the
**justfile the single source of truth**:

* `justfile` defines `lint` / `type` / `test` once; `type` is `uv run ty check` (whole
  tree). This is the only place the commands live.
* `ci.yml` lint-type-test installs `just` (pinned `extractions/setup-just`) and its steps
  call `just lint` / `just type` / `just test` — CI runs exactly what a developer runs.
  The job already `uv sync`s + installs `libvirt-dev`, so whole-tree ty has what it needs.
* `.pre-commit-config.yaml` ty hook delegates to `just type` (whole-tree), so the
  pre-commit copy can no longer diverge either.
* `ci.yml` pre-commit job keeps `SKIP: ty` (stays venv-free); the lint-type-test job is the
  one place CI type-checks `tests/`, now via the shared recipe.

Whole-tree ty in the synced CI job closes the gap; routing CI and pre-commit through the
justfile prevents the duplication that opened it from recurring.
