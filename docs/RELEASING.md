# Releasing

This project follows [ADR-0041](adr/0041-versioning-release-process.md): SemVer in the
`0.y.z` phase, milestone→minor, with the **in-tree version always pointing at the next
unreleased version** so a `-dev` build is never ambiguous across a release boundary.

## Version bumps (each via `just set-version`, which runs `uv version` to update `pyproject.toml` + `uv.lock`)

- **At a Milestone's start** — `just set-version <next-minor>` (e.g. `0.2.0` for M1), on a
  branch → PR → merge.
- **Immediately after a release** — open a `chore(release): begin <next>-dev` PR:
  `just set-version <next-patch>` and `just changelog` (the new tag now exists, so the
  `[Unreleased]` section rolls into the dated released section). This is **required** — it
  is what keeps `X.Y.Z-dev` meaning "ahead of the last release."

Never hand-edit the version: editing `pyproject.toml` alone desyncs `uv.lock` and breaks
`uv sync --locked` in CI. `just lock-check` (and CI) catch a stale lock.

## Cutting a release

1. Ensure `main` is green and `[project].version` already equals the version to release
   (it was bumped at Milestone start or by the previous post-release bump — **the release
   itself does not bump the version**).
2. From an up-to-date, clean `main`: `just release <X.Y.Z>`. This verifies state and pushes
   the annotated `vX.Y.Z` **tag only** (pushing a tag is not a commit to the protected
   branch).
3. `release.yml` triggers on the tag: it verifies tag == version, builds the wheel + sdist
   (commit SHA baked, `RELEASE=true`), generates notes from git-cliff, and creates an
   internal GitHub Release with the artifacts attached.
4. Open the post-release "begin `<next>`-dev" bump PR (above) and **merge it before any
   other PR to `main`**. Until it lands, `main` still reads the just-released version, so a
   commit merged ahead of it would report `X.Y.Z-dev` meaning "after" the release —
   reopening the ambiguity the scheme exists to prevent ([ADR-0041](adr/0041-versioning-release-process.md)
   decision 3). Treat `main` as frozen for normal merges until the bump is in.

## Commit conventions the changelog depends on

git-cliff categorizes from the commit message, so two cases need an explicit marker or they
are mis- or under-reported:

- **Breaking changes** (a renamed/removed MCP tool, a changed `ToolResponse` shape, a
  non-back-compatible migration — the contract in [ADR-0041](adr/0041-versioning-release-process.md)
  decision 1) **must** carry a `!` (`feat!: …`) or a `BREAKING CHANGE:` footer. Without it
  the change lands only in its normal group and the `⚠ Breaking Changes` heading misses it —
  and a breaking change forces a **minor** bump, so this is load-bearing.
- **Security fixes** use a `(security)` scope, e.g. `fix(security): …`, which routes them to
  the Keep-a-Changelog `Security` group (a plain `fix:` goes to `Fixed`).

## Version reporting

`python -m kdive --version` and the startup log show `X.Y.Z+g<sha>` for a release build and
`X.Y.Z-dev+g<sha>` otherwise. The SHA/flag come from a baked `_buildinfo.py` in artifacts,
or live git in a checkout.

## Future toggles (not yet enabled)

- **PyPI publish** — add a `uv publish` step to `release.yml` after the GitHub Release step.
- **Signed tags / artifact attestation** — sign `vX.Y.Z` tags and attach provenance.

## Rollback

A release is a tag + a GitHub Release; it changes no `main` history. To withdraw one, delete
the GitHub Release and the tag (`git push origin :vX.Y.Z`), fix forward, and re-tag.
