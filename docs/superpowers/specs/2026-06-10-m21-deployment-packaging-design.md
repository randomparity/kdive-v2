# Design — M2.1 Deployment & packaging

- **Date:** 2026-06-10
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
- The compose and Helm references bring the app tier up healthy against the backends.
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
   against operator-provided Postgres/MinIO/OIDC, with an off-by-default demo toggle that
   stands up bundled backends via subchart dependencies. (ADR-0088.)
4. **Migrations run as a dedicated one-shot.** Compose: a `migrate` service the app
   services `depend_on`. Helm: a pre-install/pre-upgrade Job. Not on the server startup
   path. (ADR-0088.)
5. **Image publish: GHCR via tagged release CI.** A release workflow builds and pushes to
   `ghcr.io/randomparity/kdive`, pinned by SemVer tag (ADR-0041 milestone→minor) plus
   digest. A PR job builds but does not push, to keep the Dockerfile honest on every
   change. (ADR-0088.)

## Components

### 1. Central configuration registry (ADR-0087)

A new `kdive.config` package holding the single declared source of truth for the `KDIVE_*`
contract.

- **`Setting` descriptor** — one declaration per variable: `name`, a `parse` callable
  (`str`/`int`/URL/path/bool), `default`, `required`, `secret: bool`, `processes`
  (the subset of the runnable commands `{server, worker, reconciler, migrate}` that
  consumes it), `group` (a logical category such as `database`, `objectstore`, `build`,
  `remote-libvirt`), and `help`. Build-toolchain settings are tagged `worker` with group
  `build` and validated when a build job runs, not at worker startup.
- **Aggregating registry.** Core settings are declared in `kdive.config`. **Provider
  settings stay co-located with their provider**: each provider/feature module exposes a
  module-level `SETTINGS = [...]` that the registry aggregates at import. This keeps core
  free of provider internals (preserving the portability hypothesis) while still giving one
  queryable surface. `providers/remote_libvirt/config.py` and the local-libvirt /
  fault-inject discovery modules migrate to this pattern.
- **Access.** Point-of-use code reads `config.get(SETTING)` (or a typed accessor) instead
  of `os.environ`. Resolution parses and caches once; an unparseable value raises a
  `configuration_error` naming the variable.
- **Startup validation.** Each process validates the settings it requires for its role at
  start, before opening the pool or binding a port, and fails fast with an actionable
  message: the variable, the expected shape, and a suggested fix. This is the direct
  remedy for the band's "undiagnosed environment fault" pain.
- **Generated reference.** `scripts/gen_config_reference.py` renders the registry to
  `docs/guide/reference/config.md` (alongside the generated tool reference), grouped by
  process and group, with secret-ref settings
  shown as ref-only (never a value). A drift test asserts the committed file matches the
  generated output — the same pure-registry → markdown pattern as
  `scripts/gen_tool_reference.py` / `tests/scripts/test_gen_tool_reference.py`.
- **Drift guard.** An ast-grep/ripgrep meta-test fails if any `os.environ.get("KDIVE_…")`
  read exists outside `kdive.config` (and the registered provider `SETTINGS` modules), so
  the single source of truth cannot silently rot — mirroring the M2 gate-allowlist
  meta-tests.
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
- **Configuration only.** The container takes no source-tree scripts: configuration is the
  registry-validated `KDIVE_*` env plus secret files mounted under `KDIVE_SECRETS_ROOT`.
- **Liveness probe (M2.1 scope).** Real health/readiness endpoints are M2.3. M2.1 uses a
  minimal liveness only: a TCP connect to the server port, and process supervision for
  worker/reconciler. *(Cross-milestone dependency: the richer readiness probe lands in
  M2.3 against this same deployment.)*

### 3. Compose reference (ADR-0088)

Extend the existing backend compose with a one-shot `migrate` service and the three app
services, wired purely through `KDIVE_*`. The backend services stay (they are the dev
backends). App services `depend_on` `migrate` completing.

### 4. Helm chart (ADR-0088)

A chart under `deploy/helm/kdive` deploying the three processes (Deployments + a Service
for the server) with config/secret wiring via ConfigMap/Secret, against operator-provided
backends. A pre-install/pre-upgrade migrate Job runs schema migrations once. An
off-by-default `bundledBackends` toggle pulls Postgres/MinIO subchart dependencies for a
turnkey demo.

### 5. CI build & publish (ADR-0088)

- **PR job:** build the image (no push) so the Dockerfile is exercised on every change.
- **Release workflow:** on a SemVer tag, build and push to `ghcr.io/randomparity/kdive`,
  tagged by version and pinned by digest. Actions pinned to SHA with version comments and
  scanned with `zizmor`; `persist-credentials: false`.

### 6. Retire hand-rolled app bootstrap

Per replace-don't-deprecate, remove the app-process crutches the image supersedes:

- **Removed:** the `stack` subcommand and `run_stack` supervisor; the `install-compose`
  and `print-local-env` dev helpers (and their `__main__` wiring).
- **Retained:** `migrate`, `install-fixtures`, and `seed-demo` (real operations the image
  still invokes), and the live-stack **backend** scripts/runbook (`scripts/live-stack/*`),
  which the M2/M2.5 operator-runs depend on for bringing the *backends* up.

## Decomposition

An epic plus six sub-issues, each its own work-issue cycle. Issue 1 is the spine; the rest
consume the registry.

| # | Sub-issue | Anchor |
|---|-----------|--------|
| 1 | Central config registry + generated reference + startup validation + drift guard | ADR-0087 |
| 2 | Container image (Dockerfile, one fat image, worker toolchain) | ADR-0088 |
| 3 | Compose reference — app tier + migrate one-shot over backends | ADR-0088 |
| 4 | Helm chart — app tier + optional bundled-backend subcharts + migrate Job | ADR-0088 |
| 5 | CI build/publish — PR build-only; release workflow → GHCR by tag+digest | ADR-0088 |
| 6 | Retire `stack`/`run_stack`/`install-compose`/`print-local-env` | — |

## Testing

- **Config registry:** unit tests for parse/default/required per `Setting`; a startup-
  validation test asserting a missing required variable yields a named `configuration_error`
  for each process role; the generated-reference drift test; the no-stray-`os.environ`
  drift guard.
- **Image:** a CI build asserting all four commands (`server`/`worker`/`reconciler`/
  `migrate`) start and that the image carries the worker toolchain (drgn/gdb/libvirt
  client/build tools resolve on PATH).
- **Compose/Helm:** a bring-up smoke test (compose up against backends → server liveness
  green, migrate exits 0; `helm template` + lint, and a kind-based install if feasible).
- **Bootstrap retirement:** assert the removed subcommands are gone and nothing imports
  `run_stack`.

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
