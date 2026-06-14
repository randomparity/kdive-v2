# Easier Install — PR1: Pullable, Version-Consistent Image — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the kdive image pullable without a local build — publish a rolling `:edge` image from `main` and signed images on version tags, and guard the chart's `appVersion` against the `pyproject` version so a release always has a matching image.

**Architecture:** Extend the existing `release-image.yml` (do not rename it — renaming orphans required status checks) to also trigger on `push: main`, letting `docker/metadata-action` pick `:edge`/`:sha-` on main and `:X.Y.Z`/`:X.Y`/`:latest` on tags. Add a `just chart-version-check` recipe (mirroring the existing `config-guard`/`docs-check` recipe-as-CI-step pattern) asserting `Chart.yaml appVersion == uv version --short`, and teach `just set-version` to bump `appVersion` so the guard stays green.

**Tech Stack:** GitHub Actions, `docker/metadata-action`, `docker/build-push-action`, cosign; `just`; `uv`; `yq`/`grep` for YAML field reads; `actionlint` + `zizmor` for workflow lint.

This is Workstream A of `docs/superpowers/specs/2026-06-12-easier-install-design.md`. It merges before PR2 (the demo path), which depends on `:edge` existing.

---

### Task 1: `appVersion` invariant guard (recipe + CI step)

Implements spec A3. The guard asserts `Chart.yaml appVersion` equals the `pyproject` version (`uv version --short`), so a drift fails the PR and a cut release always has a published image at the chart's default tag.

**Files:**
- Modify: `justfile` (add a `chart-version-check` recipe; add it to the `ci:` aggregate recipe — find it by content, `ci: lint type lock-check …`, since inserting the recipe shifts line numbers)
- Modify: `.github/workflows/ci.yml:80-83` (add a step after "Config env guard")
- Modify: `justfile:151-159` (`set-version` recipe — also bump `appVersion`)

- [ ] **Step 1: Add the `chart-version-check` recipe to `justfile`.** Insert after the `config-guard` recipe (near line 216):

```just
# Assert the Helm chart's appVersion tracks the pyproject version (spec A3). A drift
# would let a cut release point the chart's default image tag at a tag that was never
# published. Run in CI and `just ci`.
chart-version-check:
    #!/usr/bin/env bash
    set -euo pipefail
    pyproject="$(uv version --short)"
    chart="$(grep -E '^appVersion:' deploy/helm/kdive/Chart.yaml | sed -E 's/^appVersion:[[:space:]]*"?([^"]+)"?[[:space:]]*$/\1/')"
    if [[ "$chart" != "$pyproject" ]]; then
        echo "::error::Chart.yaml appVersion ($chart) != pyproject version ($pyproject)." >&2
        echo "Run 'just set-version $pyproject' or align Chart.yaml appVersion." >&2
        exit 1
    fi
    echo "appVersion == pyproject == $pyproject"
```

- [ ] **Step 2: Verify the recipe passes on the current tree.**

Run: `just chart-version-check`
Expected: `appVersion == pyproject == 0.3.0`

- [ ] **Step 3: Verify the recipe FAILS on a deliberate drift.**

Run:
```bash
sed -i.bak 's/^appVersion: "0.3.0"/appVersion: "9.9.9"/' deploy/helm/kdive/Chart.yaml
just chart-version-check; echo "exit=$?"
mv deploy/helm/kdive/Chart.yaml.bak deploy/helm/kdive/Chart.yaml
```
Expected: prints the `::error::` line and `exit=1`, then the file is restored.

- [ ] **Step 4: Wire the recipe into `set-version` so a bump keeps the guard green.** In `justfile` `set-version` (line 151), after the `uv version --no-sync "{{VERSION}}"` line, append a step that rewrites `appVersion`:

```just
    # Keep the Helm chart's appVersion locked to the pyproject version (spec A3 /
    # chart-version-check). Done here so a version bump never trips the CI guard.
    sed -i.bak -E 's/^appVersion:.*/appVersion: "{{VERSION}}"/' deploy/helm/kdive/Chart.yaml
    rm -f deploy/helm/kdive/Chart.yaml.bak
```

- [ ] **Step 5: Verify `set-version` updates both.**

Run:
```bash
just set-version 0.3.1 && grep -E '^appVersion' deploy/helm/kdive/Chart.yaml && uv version --short
git checkout -- pyproject.toml uv.lock deploy/helm/kdive/Chart.yaml
```
Expected: `appVersion: "0.3.1"` and `0.3.1`, then the checkout reverts the experiment.

- [ ] **Step 6: Add the CI step.** In `.github/workflows/ci.yml`, after the "Config env guard" step (ends line 83), add:

```yaml
      - name: Chart appVersion guard
        # Chart.yaml appVersion must equal the pyproject version (spec A3) so a cut
        # release publishes an image at exactly the chart's default tag. Listed here
        # because CI invokes recipes individually.
        run: just chart-version-check
```

- [ ] **Step 7: Add the recipe to the `ci` aggregate.** Find the `ci:` recipe in `justfile` by content (it reads `ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check config-docs-check config-guard test`; its line number shifted when Step 1 inserted the recipe). Append `chart-version-check` before `test`:

```just
ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check config-docs-check config-guard chart-version-check test
```

- [ ] **Step 8: Lint the workflow.**

Run: `actionlint .github/workflows/ci.yml && zizmor .github/workflows/ci.yml`
Expected: no errors.

- [ ] **Step 9: Commit.**

```bash
git add justfile .github/workflows/ci.yml
git commit -m "ci: guard chart appVersion against pyproject version"
```

---

### Task 2: Rolling `:edge` + release images from one workflow

Implements spec A1. Add a `main` trigger to `release-image.yml`, let `metadata-action` choose tags by event, sign every pushed digest, and gate SBOM/provenance to tags only.

**Files:**
- Modify: `.github/workflows/release-image.yml:6-8` (triggers), `:46` (tag rules), `:48-57` (build args), `:62-74` (sign step comment)

- [ ] **Step 1: Add the `main` trigger.** Replace the `on:` block (lines 6-8):

```yaml
on:
  push:
    branches: [main]
    tags: ["v*.*.*"]
```

- [ ] **Step 2: Replace the `Derive image tags and labels` step (lines 41-46)** so tags are event-driven:

```yaml
      - name: Derive image tags and labels
        id: meta
        uses: docker/metadata-action@80c7e94dd9b9319bd5eb7a0e0fe9291e23a2a2e9 # v6.1.0
        with:
          images: ghcr.io/randomparity/kdive
          # On main: a moving :edge plus an immutable :sha-<short>. On a v* tag:
          # :X.Y.Z, :X.Y, and :latest. NOTE: :latest is applied on EVERY release tag
          # (metadata-action does not compare against other tags), so it tracks the most
          # recently pushed release. This assumes forward-only releases; if you ever push a
          # backport tag, drop `latest` for that release so it doesn't move backward.
          tags: |
            type=edge,branch=main
            type=sha,prefix=sha-,enable={{is_default_branch}}
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}
```

- [ ] **Step 3: Gate SBOM/provenance to tags only.** Replace the `Build and push` step (lines 48-57):

```yaml
      - name: Build and push
        id: build
        uses: docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf # v7.2.0
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          # SBOM + max provenance are release artifacts; a per-commit (:edge) SBOM is
          # cost with little value. Emit them only on a v* tag.
          sbom: ${{ startsWith(github.ref, 'refs/tags/v') }}
          provenance: ${{ startsWith(github.ref, 'refs/tags/v') && 'mode=max' || 'false' }}
```

- [ ] **Step 4: Update the sign-step comment to note it signs `:edge` digests too** (the digest is immutable even though `:edge` floats). Replace lines 62-65's comment:

```yaml
      - name: Sign the published digest
        # Sign the immutable @sha256 digest, not the mutable tag (:edge floats; a tag can
        # be retargeted after signing, the digest cannot). Runs on every push — main and
        # tags — so every published digest is signed. Fail fast if the build emitted no
        # digest rather than signing an empty subject.
```

- [ ] **Step 5: Lint the workflow.**

Run: `actionlint .github/workflows/release-image.yml && zizmor .github/workflows/release-image.yml`
Expected: no errors. (If `zizmor` flags the `${{ }}` in `provenance`, it is a value expression, not a script injection — confirm it reads from `github.ref`, a trusted context, and is acceptable.)

- [ ] **Step 6: Dry-render the tag logic mentally against both events** — confirm a `main` push yields `edge` + `sha-<short>` and a `v0.3.0` tag yields `0.3.0` + `0.3` + `latest`. No command; this is a read-through of the `tags:` block.

- [ ] **Step 7: Commit.**

```bash
git add .github/workflows/release-image.yml
git commit -m "ci(release-image): publish rolling :edge from main alongside signed tags"
```

---

### Task 3: Document the public-package step and `:edge` opt-in

Implements spec A2 + A4 (docs half). The GHCR package is created private on first push; making it public is a one-time GitHub setting, and a source-checkout install must pin `:edge` because the default `appVersion` tag is the next-unreleased (unpublished) version.

**Files:**
- Modify: `docs/RELEASING.md` (the "Future toggles" / image section — document the one-time public toggle)
- Modify: `deploy/helm/kdive/README.md` (the Install section — `:edge` from a checkout)
- Modify: `docs/runbooks/kubernetes-deploy.md:1-37` (step 1 — replace "no image is published, build your own" with the published-image + `:edge` reality)

- [ ] **Step 1: Add a "Container image publishing" section to `docs/RELEASING.md`** (after the "Cutting a release" section). Use plain, factual language (no "critical/robust/comprehensive"):

```markdown
## Container image publishing

`release-image.yml` publishes the image to `ghcr.io/randomparity/kdive`:

- **Every push to `main`** → a rolling `:edge` tag and an immutable `:sha-<short>`.
- **Every `vX.Y.Z` tag** → `:X.Y.Z`, `:X.Y`, `:latest`, with an SBOM, max provenance, and
  a cosign keyless signature on the digest.

**One-time setup — make the package public.** GHCR packages are created private on first
push and visibility cannot be set from the workflow. After the first `main` push publishes
`:edge`, set the package public once: GitHub → your profile → Packages → `kdive` → Package
settings → Change visibility → Public. Until then `docker pull` returns 404 to anonymous
clients and the chart needs an `imagePullSecret`.

**Verify a release image** (not `:edge`, which floats):
`cosign verify ghcr.io/randomparity/kdive:X.Y.Z --certificate-identity-regexp '^https://github\.com/randomparity/kdive/\.github/workflows/release-image\.yml@' --certificate-oidc-issuer https://token.actions.githubusercontent.com`
```

- [ ] **Step 2: Add the `:edge` from-checkout note to `deploy/helm/kdive/README.md`** under the "Install (external backends, production)" section, before the `helm install` block:

```markdown
> **Installing from a source checkout?** The chart's default image tag is `appVersion`,
> which tracks the *next unreleased* version (ADR-0041) and has no published image until
> that version is cut — a bare install would `ImagePullBackOff`. From a checkout, pin the
> rolling image: add `--set image.tag=edge`. A bare `appVersion` default is correct only
> when you install a cut release / published chart.
```

- [ ] **Step 3: Replace `docs/runbooks/kubernetes-deploy.md` step 1's "build your own" premise.** Change the prerequisite bullet and step 1 so they reflect a published image with a `:edge` option (keep the build-your-own path as a fallback for a fully offline cluster). Replace the "A container registry…" prerequisite bullet:

```markdown
- A cluster that can pull from `ghcr.io` (the default registry). The chart defaults to
  `ghcr.io/randomparity/kdive`; `:edge` (rolling, from `main`) and signed `:X.Y.Z` release
  tags are published there. From a source checkout pin `--set image.tag=edge` (the default
  `appVersion` tag is unpublished until that version is cut). Only a fully offline cluster
  needs the build-and-load path in step 1.
```

- [ ] **Step 4: Verify the docs reference-generators still pass** (these docs are not generated, but the guard runs on the tree):

Run: `just docs-check`
Expected: PASS (no diff in generated references — this task touches only hand-written docs).

- [ ] **Step 5: Commit.**

```bash
git add docs/RELEASING.md deploy/helm/kdive/README.md docs/runbooks/kubernetes-deploy.md
git commit -m "docs: published image, one-time public toggle, and :edge from-checkout install"
```

---

## Self-review (PR1)

- **Spec coverage:** A1 → Task 2; A2 → Task 3 step 1; A3 → Task 1; A4 (docs half) → Task 3 steps 2–3. A4's chart-default behavior is unchanged code (helper already defaults to `appVersion`), so no code task is needed.
- **No image renamed:** the workflow keeps the filename `release-image.yml` (spec A1 / iteration-2 fix).
- **Activation (operational, post-merge, not a task):** push to main publishes `:edge` → make the package public (Task 3 step 1 doc) → `just release 0.3.0` publishes the first signed release image.
