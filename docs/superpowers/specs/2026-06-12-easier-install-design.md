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
version from `pyproject`, so at release time the cut tag, the `pyproject` version, and the
chart's default tag are the same string — a **released chart** never points at a missing
image. (Between releases, on a `-dev` checkout, that tag is the next-unreleased version and is
unpublished; the demo and from-checkout docs pin `:edge` instead — see A4.)

**A4. The chart default, and the source-checkout caveat.** The image helper defaults the tag
to `.Chart.AppVersion`, which is correct for a consumer installing a **cut release / published
chart** — that tag is the just-published signed image. But `appVersion` tracks `pyproject` =
the *next unreleased* version (the Problem note), so on a `main` checkout the default tag is
always a version with **no published image** — a bare `helm install deploy/helm/kdive` from
the checkout `ImagePullBackOff`s. Because kdive ships no published chart yet, every documented
install is a source-checkout install. So the from-checkout docs and the demo command pin a
pullable tag: `--set image.tag=edge` (the rolling main image from A1), shipped as a
`values-demo.yaml` so the demo stays one `-f` flag. The bare `appVersion` default is reserved
for the eventual published-chart consumer. Document `:edge` opt-in in the chart README and the
kubernetes-deploy runbook.

**Activation (operational):** merge PR1 → first push to main publishes `:edge` → flip the
package to public → cut `v0.3.0` (`just release`) → first signed release image at the chart
default tag.

### Workstream B — turnkey demo path (item 3)

Replaces ADR-0088 decision 7 (Bitnami subcharts on `emptyDir`, no OIDC) with first-party,
ephemeral, in-chart backends. **The ADR action depends on 0088's status at implementation
time:** ADR-0088 is currently `Status: Proposed`, and the supersede-don't-edit convention
applies to *accepted* decisions — so if 0088 is still Proposed, amend decision 7 (and the
bundled-backends sections) in place; only if 0088 has been accepted by then, write a new
**ADR-0097 superseding decision 7** (and add the back-reference on 0088). Check the status
first. All demo resources are gated `{{- if .Values.bundledBackends }}` and still require
`demoAcknowledged=true` (the existing render-time `fail` gate is unchanged).

**Demo prerequisites (named, not assumed).** The `minio` and `mock-oauth2-server` images ship
a glibc built for `x86-64-v2`, so the demo requires nodes whose CPU clears that floor — a
`qemu64`/`kvm64`-class guest (common in homelabs and some CI) aborts at startup with
`Fatal glibc error: CPU does not support x86-64-v2`, which reads as an inscrutable
`CrashLoopBackOff`. The chart README and `NOTES.txt` state this floor. The `helm test` smoke
pod also greps `/proc/cpuinfo` for the v2 feature set — but as a **best-effort early hint
only**: a single test pod sees just the node it lands on, so on a heterogeneous cluster it can
pass while a backend crashed on a different node. The **authoritative** CPU-floor signal is the
bounded readiness poll (B4) timing out and surfacing the crashed backend pod's
status/`lastState` (`Fatal glibc error…`). A cluster-wide guarantee would need a per-node check
(DaemonSet/node-label), which is out of scope for the demo. (This is why the kdive-dev e2e run
is gated on the node-CPU fix already in flight.)

**Demo access boundary (named, not assumed).** The bundled issuer mints valid `aud=kdive`
tokens for any request, and kdive's tool surface includes destructive power/force-crash/
teardown operations — so an exposed demo MCP is an unauthenticated control plane. The
**primary** guard is a render-time gate: when `bundledBackends=true`, the chart `fail`s if
`service.type != ClusterIP` (and the demo issuer Service is always `ClusterIP`). This is the
control that actually counters the threat — a NodePort/LoadBalancer with the default
`externalTrafficPolicy: Cluster` SNATs external traffic to a node IP, which a pod-ingress
NetworkPolicy cannot distinguish from in-cluster traffic, so a NetworkPolicy alone would give
false assurance against exactly this path. A NetworkPolicy is still shipped as
defense-in-depth (with the caveat that it is a no-op under a CNI that does not enforce it), and
`NOTES.txt` states the demo must never be exposed. "Demo-only" governs access here, not just
data durability.

**B1. Drop the subcharts.** Remove the `postgresql` and `minio` dependencies from
`Chart.yaml`, delete `Chart.lock`, drop the `helm dependency build` step from ci.yml, and
remove the chart README "Subchart distribution" section. Also remove the now-dead
`postgresql:`/`minio:` subchart-override blocks from `values.yaml` (they only configured the
Bitnami subcharts; left in place they are dead config users would set with no effect) — fold
any still-needed knob, e.g. a demo image tag, into the demo templates' own values. The chart
becomes self-contained.
Bump `Chart.yaml version` (chart semver, `0.1.0` → `0.2.0`) since the chart's templates and
dependencies change — registry consumers pin by chart version, so a no-bump would hide the
change. Policy: bump the chart `version` on any template or dependency change (distinct from
`appVersion`, which tracks the app/`pyproject` version per A3).

**B2. First-party demo templates** (new `templates/demo/`):

- `postgres.yaml` — Deployment + Service, `emptyDir`, `postgres:17`, credentials from
  `demoCredentials.postgresql`.
- `minio.yaml` — Deployment + Service, `emptyDir`, pinned `minio` image tag, credentials from
  `demoCredentials.minio`, plus a bundled-only bucket-create Job (`mc mb` for
  `KDIVE_S3_BUCKET`). The Job waits for MinIO readiness before running — an init-container
  poll or `mc` retry, mirroring the migrate Job's wait-for-db — so it does not race the MinIO
  Deployment and burn its backoff on connection-refused.
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

**B4. Verification and token.** `templates/tests/smoke.yaml` (`helm.sh/hook: test`, with
`hook-delete-policy: before-hook-creation` so repeated `helm test` runs don't collide on the
pod name) runs a pod that (1) runs the node-CPU preflight, (2) **polls the MCP `8000` Service with a bounded
retry until the server answers** — necessary because `helm install` does not `--wait` and
`migrate` is a `post-install` hook, so the server is briefly `0/1` (readiness gated on the
not-yet-migrated DB) when the test fires. The poll targets the MCP Service, not the aux
`/readyz`: the chart fronts only port `8000` with a Service, and the aux `/readyz` (9464) is
pod-local with no Service route, so a separate test pod cannot reach it. Readiness signal is
the MCP endpoint answering at all — an unauthenticated request returning `401` means the
server is up and verifying tokens; `connection-refused`/`503` means keep waiting. Then (3) the
pod mints a token from the in-cluster OIDC service and lists tools over MCP, asserting a
non-empty tools array. Without the readiness poll the one-command proof would flap on a
connection-refused/503 race.

**The list-tools call must go through the MCP session handshake, not a one-shot POST.** The
server is `FastMCP(name="kdive", auth=…)` with no `stateless_http`, so the streamable-HTTP
transport is session-based (the runbook notes a bare request yields a `307`/session error). A
lone `tools/list` POST without a prior `initialize` (which issues the `Mcp-Session-Id`) fails
even on a healthy server. So the smoke test uses the **MCP client already in the kdive image**
(it negotiates `initialize` → `list_tools()` for us); minting the bearer first with `urllib`
needs no extra tooling image. `helm test kdive` is the proof; the install docs also show
`helm install --wait` as the belt-and-suspenders option. Bundled-path only (the external path
needs the operator's real IdP). `NOTES.txt` shows the same flow — mint a token from the bundled
issuer, then drive `/mcp` with an MCP client (not a raw `tools/list` curl), and notes the
token's expiry; the external path keeps today's reach-MCP guidance.

**B5. Install becomes:**

```sh
# values-demo.yaml pins image.tag=edge + bundledBackends/demoAcknowledged so the
# demo pulls the published rolling image, not the unpublished appVersion default (A4).
helm install kdive deploy/helm/kdive -f deploy/helm/kdive/values-demo.yaml
helm test kdive          # mints a token, asserts tools/list == 200
```

Prerequisite: the demo is runnable once A has published `:edge` (or a release image
exists). Before that, there is no pullable app image and the demo cannot come up.

## Data flow (demo path)

```
helm install (bundledBackends=true)
  -> pre-install: config ConfigMap (computed DB/S3/OIDC URLs, AWS_*)
  -> normal resources: postgres, minio, oidc Deployments+Services (emptyDir)
  -> bucket-create Job (waits for minio ready, then mc mb)
  -> post-install: migrate Job (wait-for-db -> schema)
  -> server/worker/reconciler roll out; /readyz turns green once DB+S3+OIDC reachable
helm test
  -> smoke pod: node-CPU preflight (x86-64-v2, best-effort: this pod's node only)
              -> poll MCP 8000 Service until it answers (401 on unauth = up;
                 conn-refused/503 = wait) — aux /readyz has no Service route
              -> POST oidc /default/token (aud=kdive) -> bearer
              -> MCP client (initialize -> list_tools) with bearer -> assert tools[]
                 (session handshake required; a one-shot tools/list POST 307s)
```

## Testing

- **Chart render** (`tests/helm/test_helm_render.py`, extended): bundled path renders
  postgres/minio/oidc + the NetworkPolicy, computes the OIDC issuer/JWKS and `AWS_*`, and the
  OIDC/MCP Services stay `ClusterIP`; the new gate `fail`s when `bundledBackends=true` and
  `service.type != ClusterIP`; external path renders none of the demo resources and passes
  `config.*` through unchanged; the `demoAcknowledged` gate still `fail`s when unset. Pure
  `helm template`, no cluster.
- **CI guard**: the `appVersion == pyproject version` step fails on a deliberate mismatch
  (verified by breaking it once).
- **Workflow lint**: existing `actionlint` + `zizmor` cover the `release-image.yml` edits;
  `:edge` publishing is observed on the first push to main.
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

1. **PR1 (A)**: `release-image.yml` rolling+release trigger, `appVersion` guard,
   `RELEASING.md`. Merge → `:edge` pullable → package public → cut `v0.3.0`.
2. **PR2 (B)**: the ADR action (amend 0088 decision 7 if still Proposed, else new ADR-0097),
   chart demo templates, `_helpers`/configmap, `helm test`, `NOTES.txt`, render tests,
   README/runbook updates. Cites the published image.

## Files touched (preview)

`.github/workflows/release-image.yml` (add `main` trigger; not renamed), `.github/workflows/ci.yml`,
`docs/RELEASING.md`, the ADR record (`docs/adr/0088-*.md` amended if still Proposed, else new
`docs/adr/0097-in-chart-demo-backends.md`),
`deploy/helm/kdive/Chart.yaml` (version + appVersion), `deploy/helm/kdive/Chart.lock` (deleted),
`deploy/helm/kdive/values.yaml` (drop dead subchart blocks), `deploy/helm/kdive/values-demo.yaml` (new),
`deploy/helm/kdive/templates/demo/{postgres,minio,oidc}.yaml`,
`deploy/helm/kdive/templates/demo/networkpolicy.yaml`,
`deploy/helm/kdive/templates/tests/smoke.yaml`, `deploy/helm/kdive/templates/_helpers.tpl`,
`deploy/helm/kdive/templates/configmap.yaml`, `deploy/helm/kdive/templates/NOTES.txt`,
`deploy/helm/kdive/README.md`, `docs/runbooks/kubernetes-deploy.md`,
`tests/helm/test_helm_render.py`.
