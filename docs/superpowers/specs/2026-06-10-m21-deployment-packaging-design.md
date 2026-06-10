# Design — M2.1 Deployment & packaging

- **Date:** 2026-06-10
- **Owner:** D. Christensen (single accountable owner for the milestone, per the band design)
- **Milestone:** [#13 — M2.1 Deployment & packaging](https://github.com/randomparity/kdive/milestone/13)
- **Band:** Productionization & operability (M2.1–M2.4), see
  [the band design](2026-06-10-m2x-productionization-band-design.md)
- **ADRs:** [0087](../../adr/0087-config-registry.md) (central config registry),
  [0088](../../adr/0088-deployment-packaging.md) (deployment & packaging)
- **Status:** approved for planning

## Problem

Driving M2 on real hardware showed kdive is not yet operable by anyone but its author.
Standing the three processes (`server`, `worker`, `reconciler`) up means hand-rolled
bootstrap: a `stack` supervisor subcommand, `install-compose`/`print-local-env` dev
crutches, and direct knowledge of which `KDIVE_*` variables each process needs. The
configuration surface is **not discoverable**: ~25 modules each call
`os.environ.get("KDIVE_…")` at their own point of use, with no single source of truth,
no startup validation, and no generated reference. A missing or malformed variable
surfaces as a confusing downstream failure, not a named configuration error.

There is no container image, no app-tier compose, and no Helm chart. The current
`docker-compose.yml` brings up only the backends (Postgres/MinIO/OIDC) and is on no CI
path.

M2.1 closes the packaging and configuration gap: a published multi-process image, a
reference compose + Helm deployment that brings the app tier up against the backends,
and one documented `KDIVE_*` contract with a generated reference and startup validation.

## Goal / acceptance

Mirrors the band design's M2.1 exit criterion:

- The three processes start from the published image with **only** documented `KDIVE_*`
  configuration — no source-tree scripts.
- The compose and Helm references bring the app tier up healthy against the backends — where
  M2.1 "healthy" is the verifiable claim `migrate` exited 0, all three processes stay up (no
  crash-loop), and the server accepts connections (after passing startup config validation).
  A live DB round-trip / readiness probe is M2.3, not an M2.1 claim. (ADR-0088 decision 5.)
- The `KDIVE_*` contract has a generated reference, and a process fails fast with a named
  configuration error (which variable, expected shape, suggested fix) when a required
  variable is missing or malformed.

## Scope decisions (settled in brainstorming)

1. **Configuration: a central typed registry.** A single declared source of truth that
   point-of-use code reads from, that startup validation and the generated reference both
   derive from. (ADR-0087.)
2. **One fat image, remote-libvirt target.** A single image carrying the full worker
   toolchain (kernel build + drgn + gdb + libvirt client + ssh) for all three entrypoints,
   built to drive the remote-libvirt and fault-inject providers over the network.
   local-libvirt remains the dev/CI provider run from a venv on a libvirt host; it is not
   containerized here. Container privilege / per-tenant sandboxing stay deferred to M3.
   (ADR-0088.)
3. **Helm: app tier + optional bundled backends.** The chart deploys the three processes
   against operator-provided Postgres/MinIO/OIDC, with an off-by-default, `demoAcknowledged`-
   gated, ephemeral bundled-backend toggle (see §4). (ADR-0088.)
4. **Migrations run as a dedicated one-shot.** Compose: app services depend on `migrate` with
   `service_completed_successfully`. Helm: a pre-install/pre-upgrade Job (demo path orders it
   after the bundled DB). Not on the server startup path; backward-compatible expand-contract
   under rolling upgrade (see §4). (ADR-0088.)
5. **Image publish: GHCR via tagged release CI.** A release workflow builds and pushes to
   `ghcr.io/randomparity/kdive`, pinned by SemVer tag (ADR-0041 milestone→minor) plus
   digest. A PR job builds but does not push, to keep the Dockerfile honest on every
   change. (ADR-0088.)

## Components

### 1. Central configuration registry (ADR-0087)

A new `kdive.config` package holding the single declared source of truth for the `KDIVE_*`
contract.

- **`Setting` descriptor** — one declaration per variable: `name`, a `parse` callable
  (`str`/`int`/URL/path/bool), `default`, `secret: bool`, `processes` (the subset of the
  runnable commands `{server, worker, reconciler, migrate}` that consumes it), `group` (a
  logical category such as `database`, `objectstore`, `build`, `remote-libvirt`), `help`,
  and a `suggest` string (the actionable fix surfaced on a validation failure). Requiredness
  is a **`required_when` predicate**, not a flat bool, so the registry can express the
  patterns already in the tree: an opt-in provider setting (e.g.
  `KDIVE_REMOTE_LIBVIRT_CA_CERT_REF`) is required **iff** its enabling variable
  (`KDIVE_REMOTE_LIBVIRT_URI`) is set, and build-toolchain settings carry no startup
  requirement (validated when a build job runs). (ADR-0087.)
- **Aggregation via an explicit manifest.** Core settings are declared in `kdive.config`.
  **Provider settings stay co-located with their provider** (a module-level `SETTINGS =
  [...]`), but aggregation is **not** import-order-dependent: the registry holds an explicit
  manifest of setting-bearing module paths and force-loads them, so the generated reference
  and drift guard see the full set regardless of which provider a process enabled. Providers
  import the registry's `Setting` type (one-way, no cycle); the manifest lives in
  `kdive/config/`, which is outside the M2 portability gate's core prefixes, so a new
  provider's manifest line is not a gated core touch. `providers/remote_libvirt/config.py`
  and the local-libvirt / fault-inject discovery modules migrate to this pattern. (ADR-0087.)
- **Access and caching.** Point-of-use code reads `config.get(SETTING)` instead of
  `os.environ`; an unparseable value raises a `configuration_error` naming the variable.
  Resolution is **scoped, not a permanent process-global cache** — it resolves against a
  startup snapshot and exposes a reset/override seam, because ~19 test files set `KDIVE_*`
  per case via `monkeypatch.setenv` and a permanent cache would freeze the first read.
  Declaring a setting must not force an eager import-time read (the remote-libvirt config
  stays deferred to discovery/connection time). (ADR-0087.)
- **Validation has two times.** (a) **Process startup** — before opening the pool or binding
  a port, each process validates the settings whose `required_when` holds, failing fast with
  the variable, expected shape, and `suggest` fix. (b) **Provider-registration / job time** —
  opt-in provider settings validate at registration; build settings at build time. A
  *configuration* error (missing/malformed `required_when` setting for an enabled provider)
  must surface loudly and is **not** swallowed by the best-effort registration catch that
  exists for *reachability* failures. `KDIVE_LOG_LEVEL` resolves in an early bootstrap phase
  through `config.get` (logging is configured before full validation). (ADR-0087.)
- **Generated reference.** `scripts/gen_config_reference.py` renders the registry to
  `docs/guide/reference/config.md` (alongside the generated tool reference), grouped by
  process and group, secret-ref settings shown as ref-only (never a value). A drift test
  asserts the committed file matches generated output — the same pure-registry → markdown
  pattern as `scripts/gen_tool_reference.py` / `tests/scripts/test_gen_tool_reference.py`.
- **Drift guard.** A meta-test fails if any process-environment read of a `KDIVE_*` variable
  exists outside `kdive.config` and the manifest's modules. It is an **ast-grep rule over the
  access form** — catching `os.environ.get(...)`, the subscript `os.environ[...]` (used today
  at `admin/bootstrap.py`), and `os.getenv(...)`, not one string literal — with an allowlist
  for the registry's own resolution site. Activation is **atomic with the migration**: the
  guard lands with the last migrated read, or early with a shrinking allowlist that must
  reach empty before issue 1 closes. (ADR-0087.)
- **Secret + redaction reuse.** Settings marked `secret` feed the existing
  `SecretRegistry`/redaction path, so which variables are secret has one source of truth;
  the generated reference replaces the removed `print-local-env` crutch.

The registry replaces the scattered reads outright (no shim, no dual path): once a variable
is declared, its old `os.environ.get` site reads from the registry.

### 2. Container image (ADR-0088)

- **One image, multi-stage.** A builder stage resolves the `uv` environment; the final
  stage is a slim Python 3.13 base plus the worker toolchain (gcc/make/binutils, gdb,
  drgn, libvirt client libraries, openssh-client). The base image is pinned by digest.
- **Entrypoint.** `python -m kdive`, with command `server | worker | reconciler | migrate`
  — the entrypoints that already exist in `src/kdive/__main__.py`. Runs as a non-root user
  and handles `SIGTERM` cleanly (the entrypoints already install signal handlers).
- **Config-only input, plus worker writable volumes.** The container takes no source-tree
  scripts: *config* is the registry-validated `KDIVE_*` env plus secret files mounted under
  `KDIVE_SECRETS_ROOT`. But config is not the only *runtime* input — the worker does real I/O
  and needs writable, non-root-owned volumes: the build workspace (`KDIVE_BUILD_WORKSPACE`,
  `/var/lib/kdive/build`), install staging (`KDIVE_INSTALL_STAGING`), the managed SSH key dir
  (`KDIVE_SSH_KEY_DIR`), and the debug/crash dirs (`KDIVE_DEBUG_DIR`/`KDIVE_CRASH_DIR`). A
  read-only worker starts but fails the first build/install job. The server and reconciler
  need none of these. (ADR-0088.)
- **Liveness probe (M2.1 scope).** Real health/readiness endpoints are M2.3. M2.1 uses a
  minimal liveness only: a TCP connect to the server port, and process supervision for
  worker/reconciler. *(Cross-milestone dependency: the richer readiness probe lands in
  M2.3 against this same deployment.)*

### 3. Compose reference (ADR-0088)

Extend the existing backend compose with a one-shot `migrate` service and the three app
services, wired purely through `KDIVE_*`. The backend services stay (they are the dev
backends). App services depend on the migrate one-shot with
`condition: service_completed_successfully` — a bare `depends_on` waits only for *start*, so
without the completion condition the apps would hit the DB before migrations finish; a
non-zero migrate exit blocks app start. (ADR-0088.)

### 4. Helm chart (ADR-0088)

A chart under `deploy/helm/kdive` deploying the three processes (Deployments + a Service
for the server) with config/secret wiring via ConfigMap/Secret, against operator-provided
backends. A pre-install/pre-upgrade migrate Job runs schema migrations once.

- **Bundled backends are ephemeral, demo-only.** The off-by-default `bundledBackends`
  toggle pulls Postgres/MinIO subcharts on `emptyDir` (no PVC, no backup) — a pod restart
  drops state by design. To keep it off the production path, `bundledBackends: true` requires
  a co-set `demoAcknowledged: true` (the chart fails to render otherwise). Production runs
  against operator-provided backends with the toggle off.
- **Migrate-Job ordering.** The pre-install/pre-upgrade Job assumes backends **pre-exist**
  (the production path). A Helm pre-install hook runs before the chart's normal resources, so
  on the `bundledBackends` demo path the migrate is ordered **after** the bundled DB is ready
  (a post-install hook or readiness wait), not pre-install.
- **Migration rollout/rollback contract.** The runner is forward-only (ADR-0015), so under a
  rolling upgrade migrations must be **backward-compatible (expand-contract)** — tolerated by
  the still-running prior code — and rollback is **image-only** (the schema rolls forward).
  A migration that can't be made backward-compatible is a documented downtime-window release.

(ADR-0088.)

### 5. CI build & publish (ADR-0088)

- **PR job:** build the image (no push) so the Dockerfile is exercised on every change.
- **Release workflow:** on a SemVer tag, build and push to `ghcr.io/randomparity/kdive`,
  tagged by version and pinned by digest. Actions pinned to SHA with version comments and
  scanned with `zizmor`; `persist-credentials: false`.
- **Artifact provenance (in scope):** the release workflow signs the pushed digest
  (keyless/OIDC cosign) and attaches an SBOM, because the image is what the band-gate's
  non-author operator is asked to trust; the runbook verifies the signature before bring-up.
  Deeper build *reproducibility* (pinned toolchain layers) is M2.4. (ADR-0088.)

### 6. Retire hand-rolled app bootstrap

Per replace-don't-deprecate, remove the app-process crutches the image supersedes:

- **Removed:** the `stack` subcommand and `run_stack` supervisor; the `install-compose`
  and `print-local-env` dev helpers (and their `__main__` wiring).
- **Retained:** `migrate`, `install-fixtures`, and `seed-demo` (real operations the image
  still invokes), and the live-stack **backend** scripts/runbook (`scripts/live-stack/*`),
  which the M2/M2.5 operator-runs depend on for bringing the *backends* up.
- **Caller sweep (cleanup, not just deletion).** Removing the `stack` subcommand leaves dead
  references that must be updated in the same change: `justfile` (`stack-up` prints "Start
  host processes with: `python -m kdive stack`"; `stack-start`/`stack-start-daemon` run
  `scripts/live-stack/start.sh`'s host-process start) and the live-stack runbook. The sweep
  repoints these at running the image/compose app tier so no caller references the removed
  subcommand.

## Decomposition

An epic plus six sub-issues, each its own work-issue cycle. Issue 1 is the spine; the rest
consume the registry.

| # | Sub-issue | Anchor |
|---|-----------|--------|
| 1 | Central config registry: manifest aggregation, `required_when`, scoped/reset cache, two-time validation, generated reference, ast-grep drift guard (atomic activation) | ADR-0087 |
| 2 | Container image (Dockerfile, one fat image, worker toolchain) + worker writable volumes | ADR-0088 |
| 3 | Compose reference — app tier + migrate one-shot (`service_completed_successfully`) over backends | ADR-0088 |
| 4 | Helm chart — app tier + `demoAcknowledged`-gated bundled backends + migrate Job (ordering + expand-contract rollout contract) | ADR-0088 |
| 5 | CI build/publish — PR build-only; release workflow → GHCR by tag+digest; cosign signing + SBOM | ADR-0088 |
| 6 | Retire `stack`/`run_stack`/`install-compose`/`print-local-env` + caller sweep | — |

**Sequencing.** Issue 1 (registry) precedes all others — they read from it. Issue 2 (image)
precedes issues 3, 4, and 5 — compose/Helm run the image and CI publishes it. Issue 6
(retirement + caller sweep) lands after 2–4, once the image/compose app tier is the working
path the removed crutches are replaced by.

## Testing

- **Config registry:** unit tests for parse/default per `Setting`; a `required_when` test
  asserting an opt-in provider setting is required only when its enabling variable is set
  (and a missing one then yields a named `configuration_error` per process role); a test that
  a provider *configuration* error is **not** swallowed by best-effort registration; a
  cache-isolation test (per-case `monkeypatch.setenv` is honored via the reset seam); the
  generated-reference drift test; the ast-grep drift guard catching get/subscript/getenv.
- **Image:** a CI build asserting all four commands (`server`/`worker`/`reconciler`/
  `migrate`) start and that the image carries the worker toolchain (drgn/gdb/libvirt
  client/build tools resolve on PATH); a worker-without-writable-volume case fails the first
  build/install job with a clear error.
- **Compose/Helm:** a bring-up smoke test (compose up against backends → server liveness
  green, migrate exits 0 *before* apps start; `helm template` + lint); `bundledBackends: true`
  without `demoAcknowledged: true` fails to render; the bundled-backend demo path orders
  migrate after the DB; a kind-based install if feasible.
- **Migration rollout:** M2.1 is the inaugural published image, so there is no prior release
  to roll back to yet — M2.1 tests only what is executable now: `migrate` applies cleanly on a
  fresh DB and is a no-op on re-run (idempotent). The expand-contract backward-compatibility
  check (§4) is a **forward gate** keyed to a baseline tag (old image's code tolerates the new
  schema); it activates once a baseline exists, biting M2.2+ upgrades, not M2.1.
- **Publish provenance:** the release workflow produces a cosign signature + SBOM for the
  pushed digest, and signature verification succeeds.
- **Bootstrap retirement:** assert the removed subcommands are gone, nothing imports
  `run_stack`, and no `justfile`/script/runbook references `python -m kdive stack` (the
  caller sweep is complete).

## Exit-criteria mapping

| Band M2.1 exit criterion | Met by |
|--------------------------|--------|
| Three processes start from the published image with only `KDIVE_*` config, no scripts | Issues 1 (config-only, validated) + 2 (image) + 6 (scripts removed) |
| Compose/Helm reference brings the app tier up healthy | Issues 3 + 4, with the liveness probe of issue 2 |
| One documented configuration surface with a generated reference | Issue 1 |

## Non-goals (out of M2.1)

- Health/readiness endpoints and observability — **M2.3**.
- `kdivectl` and the admin surface — **M2.2**.
- Image & rootfs lifecycle management (build/validate/publish the guest images) — **M2.4**.
- Container per-tenant sandboxing and a manager-backed secret backend — **M3**.
- Containerizing local-libvirt (host-libvirt socket mount / privilege) — out of band.
