# Design: Easier install

Status: approved (brainstorming) · Date: 2026-06-12 · Branch: `feat/easier-install`

## Problem

A user following the README to install kdive on Kubernetes hits five hurdles before a
working `tools/list`:

1. **No image exists.** `release-image.yml` builds, pushes to `ghcr.io/randomparity/kdive`,
   and cosign-signs on a `v*` tag — but it has never executed. It was added after `v0.2.0`
   was tagged, and no `v*` tag has been pushed since, so no image is published for any
   version. `gh run list --workflow=release-image.yml` is empty; the GHCR package returns
   404. The user must build a ~622 MB image (compiling `libvirt-python`, installing the
   drgn wheel).
2. **Loading the image is cluster-specific.** On a registry-less cluster with no node SSH
   (k3s, kind), there is no smooth path; it takes a privileged pod that mounts the node
   containerd socket and runs `ctr images import`.
3. **Three backends are bring-your-own.** Postgres, MinIO, and an OIDC issuer must be stood
   up separately. The `bundledBackends` demo path pulls Bitnami subcharts whose Docker Hub
   images were retired in 2025, so it does not come up.
4. **The demo path has no OIDC issuer at all.** `bundledBackends` derives only the database
   and S3 endpoints; it never sets `KDIVE_OIDC_ISSUER`/`KDIVE_OIDC_JWKS_URI`, so the server
   cannot verify a bearer token and an authenticated `tools/list` is unreachable.
5. **Verification needs a hand-minted token.** Reaching `tools/list` requires an `aud=kdive`
   token, which a casual evaluator has no way to produce.

Note: the chart's `appVersion: 0.3.0` against newest tag `v0.2.0` is **not** a defect —
ADR-0041 keeps the in-tree version pointing at the next unreleased version, and `appVersion`
tracks `pyproject`. The gap is that nothing enforces `appVersion == pyproject version`, so a
release could point the chart at an image tag that was never published.

## Goals

- A one-command **demo** path that reaches an authenticated `tools/list` with no registry,
  no external backends, no IdP, and no hand-built image.
- A published, signed, public, version-consistent **release image** so the external-backend
  path installs without a build step.

## Non-goals

- PyPI publish, a `kdivectl` CLI, and a multi-arch (arm64) image — deferred.
- Durable bundled backends. The demo stays ephemeral (`emptyDir`) by design.
- Changes to the external-backend production contract beyond the image being pullable.

## Workstreams

Two independently-mergeable workstreams under one spec; A merges before B (B cites the image
A publishes). Each is its own PR.

### Workstream A — a pullable, version-consistent image (items 1 + 2)

**A1. One workflow publishes both rolling and release images.** Rename `release-image.yml`
to `image.yml` and add a `push: branches: [main]` trigger alongside the existing
`push: tags: ["v*.*.*"]`. `docker/metadata-action` selects tags by trigger:

- on `main`: `:edge` and `:sha-<short>`
- on a `v*` tag: `:X.Y.Z`, `:X.Y`, and `:latest`

Cosign keyless-sign every pushed digest. Keep the SBOM and provenance steps gated to the tag
trigger (they are release artifacts; a per-commit SBOM is unnecessary cost).

**A2. The GHCR package is public.** Package visibility cannot be set from the workflow — the
package is created private on first push. Making it public is a one-time GitHub setting,
documented as a setup step in `docs/RELEASING.md`. The chart and docs assume no pull secret.

**A3. An `appVersion` invariant guard.** A new step in the ci.yml `lint-type-test` job
asserts `Chart.yaml appVersion == uv version --short`. This fails a PR that drifts the chart
version from `pyproject`, so every cut release has a matching published image and the chart's
default tag always resolves to a real image.

**A4. The chart default is unchanged.** Default install pulls the signed release tag
(`appVersion`). `:edge` is opt-in via `--set image.tag=edge`, documented in the chart README
and the kubernetes-deploy runbook.

**Activation (operational):** merge PR1 → first push to main publishes `:edge` → flip the
package to public → cut `v0.3.0` (`just release`) → first signed release image at the chart
default tag.

### Workstream B — turnkey demo path (item 3)

Replaces ADR-0088 decision 7 (Bitnami subcharts on `emptyDir`, no OIDC) with first-party,
ephemeral, in-chart backends, recorded in **ADR-0097 superseding that decision**. All demo
resources are gated `{{- if .Values.bundledBackends }}` and still require
`demoAcknowledged=true` (the existing render-time `fail` gate is unchanged).

**B1. Drop the subcharts.** Remove the `postgresql` and `minio` dependencies from
`Chart.yaml`, delete `Chart.lock`, drop the `helm dependency build` step from ci.yml, and
remove the chart README "Subchart distribution" section. The chart becomes self-contained.

**B2. First-party demo templates** (new `templates/demo/`):

- `postgres.yaml` — Deployment + Service, `emptyDir`, `postgres:17`, credentials from
  `demoCredentials.postgresql`.
- `minio.yaml` — Deployment + Service, `emptyDir`, pinned `minio` image tag, credentials from
  `demoCredentials.minio`, plus a bundled-only bucket-create Job (`mc mb` for
  `KDIVE_S3_BUCKET`).
- `oidc.yaml` — **new** — Deployment + Service running `mock-oauth2-server` with a
  `JSON_CONFIG` that pins the `default` issuer to mint `aud=kdive` tokens deterministically.
  This is the missing piece that made the demo unable to authenticate.

**B3. Computed wiring** (`_helpers.tpl`, `configmap.yaml`): on the bundled path, derive
`KDIVE_DATABASE_URL`, `KDIVE_S3_ENDPOINT_URL`, **and now `KDIVE_OIDC_ISSUER` /
`KDIVE_OIDC_JWKS_URI`** from the in-chart service names (`<fullname>-postgres`, `-minio`,
`-oidc`). Keep emitting `AWS_*` from `demoCredentials.minio`. The external path is unchanged:
`config.*` passes through. The migrate Job keeps its existing bundled-path `post-install` +
wait-for-db behavior; only the waited host name changes to `<fullname>-postgres`.

**B4. Verification and token.** `templates/tests/smoke.yaml` (`helm.sh/hook: test`) runs a
pod that mints a token from the in-cluster OIDC service and POSTs `tools/list` to the server
service, asserting HTTP 200 and a non-empty tools array — `helm test kdive` is the one-command
proof. The test is bundled-path only (the external path needs the operator's real IdP).
`NOTES.txt` prints, on the bundled path, a copy-paste `curl` that mints a token from the
bundled issuer and calls `/mcp`; the external path keeps today's reach-MCP guidance.

**B5. Install becomes:**

```sh
helm install kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true
helm test kdive          # mints a token, asserts tools/list == 200
```

## Data flow (demo path)

```
helm install (bundledBackends=true)
  -> pre-install: config ConfigMap (computed DB/S3/OIDC URLs, AWS_*)
  -> normal resources: postgres, minio, oidc Deployments+Services (emptyDir)
  -> bucket-create Job (mc mb)
  -> post-install: migrate Job (wait-for-db -> schema)
  -> server/worker/reconciler roll out; /readyz turns green once DB+S3+OIDC reachable
helm test
  -> smoke pod: POST oidc /default/token (aud=kdive) -> bearer
              -> POST server /mcp tools/list with bearer -> assert 200 + tools[]
```

## Testing

- **Chart render** (`tests/helm/test_helm_render.py`, extended): bundled path renders
  postgres/minio/oidc and computes the OIDC issuer/JWKS and `AWS_*`; external path renders
  none of them and passes `config.*` through unchanged; the `demoAcknowledged` gate still
  `fail`s when unset. Pure `helm template`, no cluster.
- **CI guard**: the `appVersion == pyproject version` step fails on a deliberate mismatch
  (verified by breaking it once).
- **Workflow lint**: existing `actionlint` + `zizmor` cover the `image.yml` edits; `:edge`
  publishing is observed on the first push to main.
- **End-to-end**: `helm test kdive` on the kdive-dev cluster is the e2e check — it resumes
  the Phase 1–3 validation via the supported demo path instead of hand-written manifests.

## Open implementation details (resolved during planning)

- The `mock-oauth2-server` `JSON_CONFIG` schema to pin `aud=kdive` needs a short spike
  against the running image.
- The `helm test` token mint must use a URL whose `iss` matches `KDIVE_OIDC_ISSUER`; use the
  namespace-local short-DNS form so the minted `iss` and the configured issuer are identical.
- Deleting `Chart.lock` must not affect the external-path render (subcharts were bundled-only).

## Sequencing

1. **PR1 (A)**: `image.yml` rolling+release, `appVersion` guard, `RELEASING.md`. Merge →
   `:edge` pullable → package public → cut `v0.3.0`.
2. **PR2 (B)**: ADR-0097, chart demo templates, `_helpers`/configmap, `helm test`,
   `NOTES.txt`, render tests, README/runbook updates. Cites the published image.

## Files touched (preview)

`.github/workflows/image.yml` (renamed from `release-image.yml`), `.github/workflows/ci.yml`,
`docs/RELEASING.md`, `docs/adr/0097-in-chart-demo-backends.md`,
`deploy/helm/kdive/Chart.yaml`, `deploy/helm/kdive/Chart.lock` (deleted),
`deploy/helm/kdive/values.yaml`, `deploy/helm/kdive/templates/demo/{postgres,minio,oidc}.yaml`,
`deploy/helm/kdive/templates/tests/smoke.yaml`, `deploy/helm/kdive/templates/_helpers.tpl`,
`deploy/helm/kdive/templates/configmap.yaml`, `deploy/helm/kdive/templates/NOTES.txt`,
`deploy/helm/kdive/README.md`, `docs/runbooks/kubernetes-deploy.md`,
`tests/helm/test_helm_render.py`.
