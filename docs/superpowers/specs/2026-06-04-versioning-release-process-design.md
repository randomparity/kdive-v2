# Versioning policy & release process — Design

**Decisions:** [ADR-0041](../../adr/0041-versioning-release-process.md) (the policy +
process this spec realizes — created with this work) ·
**Context:** the `v0.1.0` annotated tag (M0 complete) already sketched the
milestone→minor mapping; this formalizes it. ·
**Doc-style guard applies:** use **Milestone**, never "Sprint"; plain factual prose.

## Goal

A SemVer-based versioning policy and a repeatable, tag-driven release process — in
place now, with no external (PyPI) publish yet. The immediate output is one PR that
installs the policy/process scaffolding **and** bumps the in-tree version to `0.2.0`
(M1 is in progress). The `v0.2.0` tag itself is cut later, when M1 completes.

Deliverables:

- `docs/adr/0041-versioning-release-process.md` — the decision record (SemVer,
  milestone→minor mapping, the release process summary). Status **Proposed**; added
  to the ADR index table.
- `docs/RELEASING.md` — the operational runbook (milestone-start bump, cut-a-release,
  CI behavior, future PyPI/artifact-baking toggles, rollback).
- `CHANGELOG.md` — Keep a Changelog format, generated from conventional commits by
  git-cliff; seeded with the `v0.1.0` section + an `[Unreleased]` section.
- `cliff.toml` — git-cliff config mapping commit types → changelog sections.
- `src/kdive/version.py` — `version_info()` / `full_version()`: package version +
  resolved commit SHA + release/dev flag.
- `src/kdive/__init__.py` — set `__version__` from `package_version()` (no import-time
  git subprocess; commit is resolved lazily via `version_info()`).
- `src/kdive/__main__.py` — a `--version` action and a startup log line in each of
  `server`/`worker`/`reconciler`.
- `justfile` — `set-version`, `changelog`, `release` recipes.
- `.github/workflows/release.yml` — tag-triggered build + internal GitHub Release.
- `pyproject.toml` + `uv.lock` — `[project].version` bumped `0.1.0` → `0.2.0` via
  `uv version` (which rewrites **both** atomically; see Surface → `set-version`).
- `README.md` — a one-line "Releasing" pointer to `docs/RELEASING.md`.

## Non-goals

- **No PyPI / external publish.** The release workflow builds artifacts and creates an
  internal GitHub Release only. Publishing is documented as a future one-step toggle
  in `RELEASING.md`, not implemented.
- **No artifact SHA baking.** Built wheels do not embed a generated `_buildinfo.py`;
  the commit SHA is resolved from live git (dev) or the CI env (release). Baking is
  documented as a future toggle next to PyPI publish — added when wheels are actually
  installed off-checkout.
- **No automated version bump in CI.** Bumping `[project].version` is a human act
  (`just set-version` at milestone start); CI never rewrites the version.
- **No new runtime dependency.** git-cliff runs via `uvx git-cliff@<pinned>`, matching
  how `zizmor`/`actionlint`/`pip-audit` already run — ephemeral, not in the lockfile.
- **No `.dev0`/`.rcN` PEP 440 pre-release segments.** Plain `X.Y.Z` package versions
  (matches the `0.1.0` precedent); the `-dev` marker is display-only metadata.

## Policy (ADR-0041)

- **SemVer 2.0.0**, currently in the `0.y.z` initial-development phase. The public
  contract versioning protects is: the **MCP tool surface** (tool names + the
  `ToolResponse` envelope shape), the **`ErrorCategory` taxonomy**, and the **durable
  Postgres schema + object state machines**.
- **Milestone → minor.** Each completed Milestone bumps the minor:
  `M1 → 0.2.0`, `M1.5 → 0.3.0`, `M2 → 0.4.0`, … first GA `→ 1.0.0`. Patch (`z`) is for
  fixes/hardening cut between Milestones (e.g. a red-team hardening release).
- **What forces which bump (the `0.y` contract rule).** A change that is
  backward-**incompatible** to the public contract above — a renamed/removed MCP tool
  or changed `ToolResponse` shape, a removed/renamed `ErrorCategory`, or a **forward-only
  migration that is not backward compatible** with the prior running server (a dropped
  column, a non-nullable add without default, a state-machine edge removal) — is
  released as a **minor** and called out in the changelog under a `Breaking` heading.
  A **patch** (`z`) may contain only **additive/backward-compatible** changes
  (additive migrations, new optional tool fields, fixes). A schema migration therefore
  does **not** automatically force a minor — an additive one is patch-eligible; a
  breaking one forces a minor. Pre-1.0, none of this is a major bump; `1.0.0` is the
  first release where a breaking contract change would require a major.
- **In-tree version leads the tag.** `[project].version` is bumped to a Milestone's
  target minor **when that Milestone's work begins**; the matching annotated
  `vX.Y.Z` tag is cut **when the Milestone completes**. Consequence: throughout a
  Milestone every build reports `X.Y.Z-dev+g<sha>`; only the tagged release commit
  reports `X.Y.Z`.
- **Tags are annotated**, named `vX.Y.Z` (matches the existing `v0.1.0`).
- **Single source of version truth:** `[project].version` in `pyproject.toml`. `uv.lock`
  carries a *synchronized copy* of the project version (`uv.lock` pins
  `name = "kdive" / version = …`), so the version is never hand-edited in isolation —
  it is changed only through `uv version`, which rewrites both files together, and a
  guard test asserts they agree (see Testing). Runtime code reads the version via
  `importlib.metadata`, which reflects the **installed** distribution; the consistency
  guard plus the fact that every task runs under `uv run`/`uv sync` (which reinstalls
  on a pyproject change) keep installed-metadata == `pyproject` == `uv.lock`.

## Surface

### `src/kdive/version.py`

The version-info resolver. Pure, lazy, cached — no subprocess at import time.

- `package_version() -> str` — `importlib.metadata.version("kdive")`; reflects the
  **installed** distribution (kept == `pyproject` by the consistency guard + `uv sync`).
  A `PackageNotFoundError` falls back to `"0.0.0"`.
- `version_info() -> VersionInfo` — a frozen dataclass `(version: str, commit: str |
  None, is_release: bool)`. Memoized with `functools.lru_cache` so it is computed once
  per process, **and exposes `version_info.cache_clear()`** — the test suite calls it
  in an autouse fixture between cases so per-case env/git mocks are not masked by a
  stale memo (without this, the cache returns the first case's result for every later
  case). The long-running processes never need to invalidate it (the version of a
  running process does not change).
- `full_version() -> str` — the display string:
  - release, commit known → `f"{version}+g{commit}"`
  - dev, commit known → `f"{version}-dev+g{commit}"`
  - commit unknown → `f"{version}-dev"`

**Commit + release resolution** (in order; first hit wins):

1. **CI/env** — if `KDIVE_BUILD_RELEASE` is truthy: `is_release=True`,
   `commit=os.environ["KDIVE_BUILD_COMMIT"]` (the release workflow sets both).
2. **Live git** — `commit = git rev-parse --short HEAD`. `is_release` is true iff
   `git describe --tags --exact-match HEAD` equals `v{package_version}` **and**
   `git status --porcelain` is empty (clean tree). All git calls are guarded
   (`FileNotFoundError`, non-zero exit, not-a-repo → fall through), short-timeout,
   and never raise to the caller.
3. **Unknown** — `commit=None`, `is_release=False` (fail toward `-dev`).

A dirty tree on the exact tag resolves to **dev** (it is not a clean release build).

### `src/kdive/__init__.py`

```python
from kdive.version import package_version
__version__ = package_version()
```

`__version__` is set from `package_version()` — `importlib.metadata` only, **no git
subprocess** — so `import kdive` stays cheap and cannot fail on a missing/odd git. The
commit and the `-dev`/release decision (which *do* shell out to git on the live-git
path) are resolved lazily, only when `full_version()` / `version_info()` is actually
called — at `--version` and at process startup, never at import. There is no eager
`__commit__` attribute for this reason; read `version_info().commit` on demand.

### `src/kdive/__main__.py`

- A top-level `--version` argument (argparse `action="version"`,
  `version=full_version()`) that prints `kdive {full_version()}` and exits before
  the subcommand is required.
- Each subcommand runner (`_run_server`/`_run_worker`/`_run_reconciler`) logs
  `"starting kdive %s", full_version()` at boot, after `configure_logging`
  (ADR-0014) and before opening the pool.

### `cliff.toml`

git-cliff config:

- Conventional-commit parsing; group by type → Keep a Changelog sections:
  `feat → Added`, `fix → Fixed`, `refactor/perf → Changed`, `docs → Documentation`,
  `chore(release)` and merge commits skipped.
- `tag_pattern = "v[0-9]*"`; output Keep a Changelog header with an `[Unreleased]`
  section for commits past the last tag.
- Pinned invocation: `uvx git-cliff@<pinned-current-stable>` (version looked up at
  implementation time, never assumed).

### `CHANGELOG.md`

Keep a Changelog header + an `[Unreleased]` section + a `[0.1.0]` section derived
from history up to the `v0.1.0` tag. Regenerated by `just changelog`; it is a
generated artifact, not hand-maintained line-by-line.

### `justfile` recipes

- `set-version VERSION` — validate `VERSION` matches `MAJOR.MINOR.PATCH`, then run
  `uv version "$VERSION"` (used at Milestone start). This rewrites **both**
  `pyproject.toml` and `uv.lock` atomically — editing `pyproject.toml` alone would
  desynchronize the project pin in `uv.lock` and make `uv sync --locked` (CI's first
  step and `just sync`) fail with a lockfile-out-of-date error. The recipe leaves both
  files changed for the human to commit on a branch (never on `main`).
- `changelog` — regenerate `CHANGELOG.md` from git history via `uvx git-cliff`.
- `release VERSION` — the Milestone-completion recipe. Guards, in order:
  on `main`, clean tree, up to date with `origin/main`, and `[project].version ==
  VERSION`. On pass: create annotated tag `vVERSION` (message names the Milestone)
  and `git push origin vVERSION` — **the tag only, never a commit to `main`.**

### `.github/workflows/release.yml`

- **Trigger:** `push: tags: ['v*']`. `permissions: contents: write` (create the
  Release). Actions pinned to SHA with version comments; `persist-credentials: false`
  — so `zizmor` stays green.
- **Concurrency:** a `concurrency` group keyed on the tag ref (`release-${{ github.ref }}`,
  `cancel-in-progress: false`) so two pushes of the same tag never race to create the
  Release.
- **Steps:**
  1. checkout **with `fetch-depth: 0`** — git-cliff needs the full commit history and
     all prior tags; the default shallow (`fetch-depth: 1`, no tags) makes
     `git-cliff --latest` emit empty/incorrect notes since it cannot find the previous
     tag to diff against.
  2. set up uv (pinned, as in `ci.yml`).
  3. **verify** the tag name equals `v{[project].version}` (fail the release on
     mismatch).
  4. `uv build` (wheel + sdist; kdive is pure-Python under `uv_build`, so no
     `libvirt-dev` needed).
  5. generate release notes via `uvx git-cliff --latest --strip header` and **assert
     the notes file is non-empty** (fail otherwise — an empty changelog means the
     history/tags were not fetched).
  6. create-or-update the Release idempotently: `gh release create "$TAG"
     --notes-file … dist/*`, falling back to `gh release edit "$TAG" --notes-file …`
     plus `gh release upload "$TAG" dist/* --clobber` if the Release already exists
     (so a re-run does not hard-fail). No publish step.

## Data flow

```
milestone start:  just set-version 0.2.0   → pyproject [project].version = 0.2.0
                                              (in-tree leads the tag)
during milestone: every build  → full_version() = "0.2.0-dev+g<sha>"   (live git, not on tag)
milestone done:   PR-merge to main, then  just release 0.2.0
                    → guards pass → annotated tag v0.2.0 → push tag
                    → release.yml: tag==pyproject check → uv build → git-cliff notes
                      → gh release create (internal, artifacts attached)
                    → on the v0.2.0 commit, full_version() = "0.2.0+g<sha>"  (is_release)
```

`CHANGELOG.md` is refreshed via `just changelog` in normal feature PRs; the GitHub
Release notes are generated fresh from git-cliff in CI at tag time, so the release
notes never depend on a committed changelog being current.

## Error handling

- **`version.py` never raises to callers.** Every git subprocess is guarded and
  falls through to the next resolution layer; the terminal fallback is
  `("0.0.0"/package_version, None, False)` → `-dev` (or `-dev` with no `+g`). Version
  reporting must not crash a process startup or `--version`.
- **`just release` fails closed.** Any failed guard (wrong branch, dirty tree, behind
  origin, version mismatch) aborts before the tag is created — no half-made release.
- **`release.yml` tag/version mismatch** fails the job, so a tag that disagrees with
  `pyproject` never produces a Release.

## Testing

- `tests/test_version.py` — unit-test `version_info()`/`full_version()` across the
  resolution layers with the git subprocess and `importlib.metadata.version` mocked
  at the boundary: (a) `KDIVE_BUILD_RELEASE=1` → release; (b) live git on the exact
  tag, clean → release; (c) on the tag but dirty → dev; (d) untagged commit → dev;
  (e) git absent / not a repo → unknown → `-dev` with no `+g`. Assert the exact
  `full_version()` string for each. An **autouse fixture calls
  `version_info.cache_clear()`** before each case so the memo from one case never
  masks the next.
- `tests/test_version_pyproject_consistency.py` — the drift guard. Parse
  `[project].version` from `pyproject.toml` and assert it equals (1)
  `version.package_version()` (installed metadata == working tree) **and** (2) the
  `kdive` project version pinned in `uv.lock`. (2) catches a `pyproject` bump that
  forgot to re-lock — the failure mode that would otherwise surface as a broken
  `uv sync --locked` in CI. The test reads the installed metadata under `uv run`, so
  the editable install is already resynced to the current `pyproject`.
- `cliff.toml` smoke: a test (or the `changelog` recipe in CI dry-run) that
  `uvx git-cliff` parses the config and emits non-empty output for the current
  history.
- `release.yml` is covered by the existing `just lint-workflows` gate
  (`actionlint` + `zizmor`); no new test infra.

## Out of scope / future toggles (documented in `RELEASING.md`)

- **PyPI publish** — add a `uv publish` step (trusted publishing / token) to
  `release.yml` after the GitHub Release step.
- **Artifact SHA baking** — generate `src/kdive/_buildinfo.py` (gitignored) before
  `uv build` so installed wheels self-report their commit without the env var; add a
  resolution layer in `version.py` that reads it ahead of live git.
- **Signed tags / artifact attestation** — sign `vX.Y.Z` tags and attach build
  provenance.
