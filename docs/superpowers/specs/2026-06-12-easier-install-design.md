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

**A1. One workflow publishes both rolling and release images.** Add a
`push: branches: [main]` trigger to the existing `release-image.yml` alongside its
`push: tags: ["v*.*.*"]`. **Keep the filename** — renaming the workflow orphans any
branch-protection rule that requires its check by name (the old check stays "Expected" and
blocks merges, or silently stops gating). `docker/metadata-action` selects tags by trigger:

- on `main`: `:edge` and `:sha-<short>`
- on a `v*` tag: `:X.Y.Z`, `:X.Y`, and `:latest`

Cosign keyless-sign every pushed digest. Keep the SBOM and provenance steps gated to the tag
trigger (they are release artifacts; a per-commit SBOM is unnecessary cost). The `:edge` tag
floats, so a digest pushed under it is verifiable but the tag is not — docs tell users to
`cosign verify` release tags, not `:edge`.

Adding the `main` trigger widens what runs on every main commit: this workflow already holds
`packages: write` + `id-token: write`, so each push now mints an OIDC token and writes the
package. That is the intended cost of a rolling image; the permission set is unchanged and
already least-privilege. `concurrency` stays per-ref with `cancel-in-progress: false`, so
rapid main pushes serialize — acceptable for a moving tag (last finisher wins; the next push
reconciles `:edge`).

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

**Demo prerequisites (named, not assumed).** The `minio` and `mock-oauth2-server` images ship
a glibc built for `x86-64-v2`, so the demo requires nodes whose CPU clears that floor — a
`qemu64`/`kvm64`-class guest (common in homelabs and some CI) aborts at startup with
`Fatal glibc error: CPU does not support x86-64-v2`, which reads as an inscrutable
`CrashLoopBackOff`. The chart README and `NOTES.txt` state this floor, and the `helm test`
smoke pod runs a node-CPU preflight first (greps `/proc/cpuinfo` for the v2 feature set) so
the failure surfaces as an actionable message, not a glibc abort. (This is why the kdive-dev
e2e run is gated on the node-CPU fix already in flight.)

**Demo access boundary (named, not assumed).** The bundled issuer mints valid `aud=kdive`
tokens for any request, and kdive's tool surface includes destructive power/force-crash/
teardown operations. The demo issuer Service is therefore `ClusterIP`-only, the chart ships a
NetworkPolicy restricting the demo OIDC/MCP to in-cluster sources on the bundled path, and
`NOTES.txt` warns that the demo MCP must never be exposed via NodePort/LoadBalancer/Ingress
(the external-backend path, with a real IdP, is the only exposure-safe path). "Demo-only"
governs access here, not just data durability.

**B1. Drop the subcharts.** Remove the `postgresql` and `minio` dependencies from
`Chart.yaml`, delete `Chart.lock`, drop the `helm dependency build` step from ci.yml, and
remove the chart README "Subchart distribution" section. The chart becomes self-contained.
Bump `Chart.yaml version` (chart semver, `0.1.0` → `0.2.0`) since the chart's templates and
dependencies change — registry consumers pin by chart version, so a no-bump would hide the
change. Policy: bump the chart `version` on any template or dependency change (distinct from
`appVersion`, which tracks the app/`pyproject` version per A3).

**B2. First-party demo templates** (new `templates/demo/`):

- `postgres.yaml` — Deployment + Service, `emptyDir`, `postgres:17`, credentials from
  `demoCredentials.postgresql`.
- `minio.yaml` — Deployment + Service, `emptyDir`, pinned `minio` image tag, credentials from
  `demoCredentials.minio`, plus a bundled-only bucket-create Job (`mc mb` for
  `KDIVE_S3_BUCKET`).
- `oidc.yaml` — **new** — Deployment + `ClusterIP` Service running `mock-oauth2-server`. A
  `JSON_CONFIG` (verified against `3.0.3`) pins the `default` issuer to mint `aud=kdive`
  deterministically via a wildcard token callback:

  ```json
  {"interactiveLogin": false,
   "tokenCallbacks": [{"issuerId": "default",
     "requestMappings": [{"requestParam": "grant_type", "match": "*",
       "claims": {"sub": "kdive-demo", "aud": ["kdive"]}}]}]}
  ```

  The server's `audience()` reads the `aud` claim from the matched mapping, so any
  `/default/token` request returns `aud=["kdive"]`. Fallback if this image ever regresses: a
  tiny static-JWKS issuer serving a fixed signed token — but the mechanism above is confirmed,
  so it is not the planned path. This closes the gap that left the demo unable to authenticate.

**B3. Computed wiring** (`_helpers.tpl`, `configmap.yaml`): on the bundled path, derive
`KDIVE_DATABASE_URL`, `KDIVE_S3_ENDPOINT_URL`, **and now `KDIVE_OIDC_ISSUER` /
`KDIVE_OIDC_JWKS_URI`** from the in-chart service names (`<fullname>-postgres`, `-minio`,
`-oidc`). Keep emitting `AWS_*` from `demoCredentials.minio`. The external path is unchanged:
`config.*` passes through. The migrate Job keeps its existing bundled-path `post-install` +
wait-for-db behavior; only the waited host name changes to `<fullname>-postgres`.

**B4. Verification and token.** `templates/tests/smoke.yaml` (`helm.sh/hook: test`) runs a
pod that (1) runs the node-CPU preflight, (2) **polls the server `/readyz` with a bounded
retry until ready** — necessary because `helm install` does not `--wait` and `migrate` is a
`post-install` hook, so the server is briefly `0/1` (readiness gated on the not-yet-migrated
DB) when the test fires — then (3) mints a token from the in-cluster OIDC service and POSTs
`tools/list` to the server service, asserting HTTP 200 and a non-empty tools array. Without
the readiness poll the one-command proof would flap on a connection-refused/503 race.
`helm test kdive` is the proof; the install docs also show `helm install --wait` as the
belt-and-suspenders option. The test runs the kdive image (Python `urllib` for the token mint
+ MCP call — no extra tooling image). Bundled-path only (the external path needs the
operator's real IdP). `NOTES.txt` prints, on the bundled path, a copy-paste `curl` that mints
a token from the bundled issuer and calls `/mcp`, and notes the token's expiry; the external
path keeps today's reach-MCP guidance.

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
  -> smoke pod: node-CPU preflight (x86-64-v2 floor) -> fail-fast if unmet
              -> poll server /readyz until ready (bounded retry; covers the
                 post-install migrate + no-`--wait` race)
              -> POST oidc /default/token (aud=kdive) -> bearer
              -> POST server /mcp tools/list with bearer -> assert 200 + tools[]
```

## Testing

- **Chart render** (`tests/helm/test_helm_render.py`, extended): bundled path renders
  postgres/minio/oidc + the NetworkPolicy, computes the OIDC issuer/JWKS and `AWS_*`, and the
  OIDC/MCP Services stay `ClusterIP`; external path renders none of the demo resources and
  passes `config.*` through unchanged; the `demoAcknowledged` gate still `fail`s when unset.
  Pure `helm template`, no cluster.
- **CI guard**: the `appVersion == pyproject version` step fails on a deliberate mismatch
  (verified by breaking it once).
- **Workflow lint**: existing `actionlint` + `zizmor` cover the `image.yml` edits; `:edge`
  publishing is observed on the first push to main.
- **End-to-end**: `helm test kdive` on the kdive-dev cluster is the e2e check — it resumes
  the Phase 1–3 validation via the supported demo path instead of hand-written manifests.

## Open implementation details (resolved during planning)

- The `mock-oauth2-server` `aud=kdive` pinning is confirmed (B2 `JSON_CONFIG` wildcard token
  callback against `3.0.3`); the spike is closed. Remaining detail is wiring the config in as
  env vs a mounted ConfigMap file.
- The `helm test` token mint must use a URL whose `iss` matches `KDIVE_OIDC_ISSUER`; use the
  namespace-local short-DNS form so the minted `iss` and the configured issuer are identical.
- Deleting `Chart.lock` must not affect the external-path render (subcharts were bundled-only);
  the render test asserts the external path is unchanged.
- The node-CPU preflight needs a check that works without extra tooling — `grep` of
  `/proc/cpuinfo` flags (`sse4_2`, `popcnt`) from the kdive image, surfaced as a clear failure.

## Sequencing

1. **PR1 (A)**: `image.yml` rolling+release, `appVersion` guard, `RELEASING.md`. Merge →
   `:edge` pullable → package public → cut `v0.3.0`.
2. **PR2 (B)**: ADR-0097, chart demo templates, `_helpers`/configmap, `helm test`,
   `NOTES.txt`, render tests, README/runbook updates. Cites the published image.

## Files touched (preview)

`.github/workflows/image.yml` (renamed from `release-image.yml`), `.github/workflows/ci.yml`,
`docs/RELEASING.md`, `docs/adr/0097-in-chart-demo-backends.md`,
`deploy/helm/kdive/Chart.yaml` (version + appVersion), `deploy/helm/kdive/Chart.lock` (deleted),
`deploy/helm/kdive/values.yaml`, `deploy/helm/kdive/templates/demo/{postgres,minio,oidc}.yaml`,
`deploy/helm/kdive/templates/demo/networkpolicy.yaml`,
`deploy/helm/kdive/templates/tests/smoke.yaml`, `deploy/helm/kdive/templates/_helpers.tpl`,
`deploy/helm/kdive/templates/configmap.yaml`, `deploy/helm/kdive/templates/NOTES.txt`,
`deploy/helm/kdive/README.md`, `docs/runbooks/kubernetes-deploy.md`,
`tests/helm/test_helm_render.py`.
