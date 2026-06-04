# ADR 0041 — Versioning policy & release process

- **Status:** Proposed
- **Date:** 2026-06-04
- **Realized by:** [`../superpowers/specs/2026-06-04-versioning-release-process-design.md`](../superpowers/specs/2026-06-04-versioning-release-process-design.md)
  (the implementation surface — `version.py`, `cliff.toml`, the justfile recipes,
  `release.yml`, `RELEASING.md` — this ADR's decisions drive)
- **Context:** the `v0.1.0` annotated tag (M0 complete) already sketched the
  milestone→minor mapping; this ADR formalizes it before M1's `v0.2.0`.
- **Relates to:** [ADR-0001](0001-greenfield-rewrite.md) (Python project this versions),
  the Milestone roadmap in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

The project tags releases (`v0.1.0` exists) but has no recorded versioning policy or
repeatable release process — the rule lived only in a tag message. As M1 nears completion
we need to cut `v0.2.0`, and the user wants version info to carry a commit SHA and a
non-release marker. Without a decision record the choices (what a version protects, when
the minor moves, how the in-tree version relates to the tag, how a build reports its SHA)
get re-argued every review pass; this ADR pins them so they converge. The operational
runbook lives in `docs/RELEASING.md`; this ADR owns the *decisions*, not the steps.

## Decision

### 1. SemVer 2.0.0 in the `0.y.z` initial-development phase

We follow SemVer 2.0.0. The project is pre-1.0, so the `0.y.z` rules apply. The public
contract versioning protects is explicitly: the **MCP tool surface** (tool names + the
`ToolResponse` envelope shape), the **`ErrorCategory` taxonomy**, and the **durable
Postgres schema + object state machines**. Other internals are not part of the contract.

### 2. Milestone → minor; the `0.y` bump rule

Each completed Milestone bumps the minor: `M1 → 0.2.0`, `M1.5 → 0.3.0`, `M2 → 0.4.0`, …
first GA `→ 1.0.0`. Within `0.y`:

- A change **backward-incompatible** to the contract in decision 1 — a renamed/removed
  tool or changed `ToolResponse` shape, a removed/renamed `ErrorCategory`, or a
  forward-only migration not backward compatible with the prior running server (dropped
  column, non-nullable add without default, state-machine edge removal) — is a **minor**
  bump, listed in the changelog under a `Breaking` heading.
- A **patch** (`z`) carries only additive/backward-compatible changes (additive
  migrations, new optional tool fields, fixes) — e.g. a hardening release between
  Milestones.
- A schema migration does **not** automatically force a minor: additive is patch-eligible,
  breaking forces a minor. Pre-1.0 nothing is a major bump; `1.0.0` is the first release
  where a breaking contract change would require a major.

### 3. In-tree version always points at the next *unreleased* version

`[project].version` is always strictly greater than the most recent tag — it names the
version being worked toward, never one already released. We keep plain `X.Y.Z` strings
(no PEP 440 `.devN`/`.rcN` segments, matching the `0.1.0` precedent); a `-dev` marker is
display-only metadata, not part of the package version. Two bumps move the in-tree version
forward:

- **Milestone start** — `set-version` jumps to the Milestone's target minor. (The first
  application, `0.1.0 → 0.2.0`, is a one-time catch-up applied *mid-M1*, since this policy
  lands while M1 is already in progress; "bump at Milestone start" is the go-forward rule,
  first observed cleanly at M1.5.)
- **Immediately after any release tag** — a `chore(release): begin <next>-dev` change
  bumps to the next patch dev version (`0.2.0 → 0.2.1` right after `v0.2.0`). A later
  Milestone-start bump overrides it (`0.2.1 → 0.3.0`).

Consequence: a non-tag build reports `X.Y.Z-dev` for the *next* version, and only the
tagged release commit reports a bare `X.Y.Z`. `0.2.0-dev` therefore only ever means
"before the `v0.2.0` release," never after it — the marker is unambiguous and ordered.
Tags are **annotated**, named `vX.Y.Z`.

### 4. One version source of truth: `pyproject` `[project].version`

`[project].version` is the single version literal. `uv.lock` carries a synchronized copy
of the project pin, so the version is changed only through `uv version` (which rewrites
both files and re-locks); a guard catches a stale committed lock. Runtime code reads the
version via `importlib.metadata` (the installed distribution), which the consistency guard
and `uv sync` keep equal to `pyproject` and `uv.lock`.

### 5. Version info carries a commit SHA and a release/dev marker, baked into artifacts

`full_version()` reports `X.Y.Z+g<sha>` for a release build and `X.Y.Z-dev+g<sha>`
otherwise, resolving commit + release status from, in order: a baked `_buildinfo.py`
(written into the artifact at build time), live git (dev checkout), or unknown (`-dev`,
no SHA). Baking is a generate-then-build step (`uv_build` packages the generated module,
verified empirically) — no dynamic-version build backend. The generated file never
persists in the editable checkout. This makes the SHA and the release/dev distinction
hold everywhere — checkout, dev-built wheel, and release wheel — not only in a git
checkout.

### 6. Releases are tag-driven, with no commit to `main` and no external publish yet

By the time a release is cut, `[project].version` is **already** at the target — the
bump happened earlier (decision 3: at Milestone start, or as the post-release bump of the
previous release). So cutting a release does **not** bump the version; it is **tag-only**:
on a reviewed, merged `main` whose `[project].version` already equals `X.Y.Z`, push the
annotated `vX.Y.Z` **tag** (the only `main`-side action — pushing a tag ref is not a commit
to the protected branch). A `release.yml` workflow triggers on the tag, verifies tag ==
`pyproject` version, builds the wheel + sdist (SHA baked, `RELEASE=True` passed
explicitly), generates release notes from conventional-commit history (git-cliff), and
creates an internal GitHub Release with the artifacts attached.

The committed `CHANGELOG.md` is regenerated by the **post-release bump PR** (decision 3),
which runs `just changelog` once the new tag exists so its `[Unreleased]` section rolls
into a dated `[X.Y.Z]` section. That file is cosmetic — the authoritative release notes are
the ones CI generates fresh from git-cliff at tag time — so a release never depends on it
being current.

**No PyPI / external publish** — that is a documented future toggle.

## Consequences

- One record now pins the versioning and release decisions, so reviews converge instead
  of re-litigating the scheme each pass (the failure mode [ADR-0040](0040-admission-lifecycle-concurrency.md)
  also calls out). The spec and `RELEASING.md` reference these decisions rather than
  re-arguing them.
- The "in-tree leads the tag, points at next-unreleased" rule (decision 3) obliges a
  post-release bump PR after every tag; `RELEASING.md` makes it a checklist step, because
  skipping it would resurrect an ambiguous `X.Y.Z-dev`.
- Version changes must go through `uv version` (decision 4); a hand-edited `pyproject`
  version that desynchronizes `uv.lock` breaks `uv sync --locked` in CI.
- Baking (decision 5) adds a build step and a generated-file cleanup obligation, and ties
  artifact correctness to `uv_build` continuing to package the generated module — guarded
  by a CI inclusion test.
- The contract definition (decision 1) gives future work a concrete test for "is this a
  breaking change," but also obliges changelog discipline (the `Breaking` heading) and a
  judgment call per change.

## Alternatives considered

- **Leave the policy in the tag message / unwritten.** Rejected: it caused the version
  scheme to be re-argued across review passes; an ADR is the convergence anchor.
- **PEP 440 `.devN+g<sha>` dynamic versions via setuptools-scm / a uv dynamic-version
  backend.** Rejected for now: it would replace `uv_build` or add a backend plugin for a
  pre-publish project; plain strings + a generated `_buildinfo.py` meet the SHA/`-dev`
  requirement with less machinery (decisions 3, 5).
- **In-tree version trails the tag (bump only at release).** Rejected: a hand-edit-only
  bump desyncs `uv.lock` and, without a post-release bump, makes `X.Y.Z-dev` ambiguous
  across the release boundary (decision 3).
- **Resolve the commit SHA from live git only (no baking).** Rejected: an installed
  artifact off a checkout has no `.git`, so it would report `-dev` with no SHA — violating
  the requirement exactly where artifacts run (decision 5).
- **A release that commits the changelog/bump directly to `main`, or tags from CI.**
  Rejected: direct `main` commits violate branch protection; tag-only push from a reviewed,
  merged `main` keeps the protection while still triggering the release (decision 6).
- **PyPI publish now.** Out of scope: no external consumer yet; the workflow is built so a
  `uv publish` step is a one-line future addition.
