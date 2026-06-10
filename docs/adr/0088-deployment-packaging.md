# ADR 0088 — Deployment & packaging: one multi-process image, compose + Helm reference (M2.1)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0035](0035-walking-skeleton-e2e-harness.md) (the
  backend-only dev compose this extends with the app tier), [ADR-0041](0041-versioning-release-process.md)
  (SemVer, milestone→minor — the image tag scheme), [ADR-0076](0076-remote-libvirt-provider-package.md)
  (the remote-libvirt provider the image is built to drive), [ADR-0087](0087-config-registry.md)
  (the registry-validated `KDIVE_*` config the container takes as its only input).
- **Spec:** [`../superpowers/specs/2026-06-10-m21-deployment-packaging-design.md`](../superpowers/specs/2026-06-10-m21-deployment-packaging-design.md)
- **Milestone:** #13 (M2.1)

## Context

There is no container image, no app-tier compose, and no Helm chart. Standing the three
processes up requires hand-rolled bootstrap (a `stack` supervisor subcommand,
`install-compose`/`print-local-env` helpers, and host knowledge of each process's
configuration). The band's M2.1 exit criterion is that the three processes start from a
published image with only documented `KDIVE_*` config — no source-tree scripts — and that a
reference compose/Helm deployment brings the app tier up healthy against the existing
Postgres/MinIO/OIDC backends.

Two facts shape the packaging:

- The `server`/`worker`/`reconciler`/`migrate` entrypoints already exist
  (`python -m kdive <cmd>`, `src/kdive/__main__.py`); M2.1 packages them, it does not
  create them.
- The worker carries a heavy runtime (kernel build toolchain, drgn, gdb, libvirt client,
  ssh); the server is light. local-libvirt — the dev/CI provider — needs a libvirt daemon
  it cannot host inside a container, whereas remote-libvirt talks `qemu+tls` to a remote
  host and runs fine containerized.

## Decision

1. **One image, multi-stage, remote-libvirt target.** A single image for all three
   entrypoints. A builder stage resolves the `uv` environment; the final stage is a slim
   Python 3.13 base plus the worker toolchain (gcc/make/binutils, gdb, drgn, libvirt client
   libraries, openssh-client), base pinned by digest. The image is built to drive the
   remote-libvirt and fault-inject providers over the network. local-libvirt stays the
   dev/CI provider run from a venv on a libvirt host; it is not containerized here.
   Container per-tenant sandboxing and privilege stay deferred to M3.

2. **Entrypoint and process model.** `ENTRYPOINT python -m kdive`, command
   `server | worker | reconciler | migrate`. Runs as a non-root user; relies on the
   existing `SIGTERM` handling in the entrypoints.

3. **Configuration is the only input.** No source-tree scripts in the container.
   Configuration is the registry-validated `KDIVE_*` env (ADR-0087) plus secret files
   mounted under `KDIVE_SECRETS_ROOT`.

4. **Migrations run as a dedicated one-shot.** Compose: a `migrate` service the app
   services `depend_on`. Helm: a pre-install/pre-upgrade Job. Migrations are not on the
   server startup path — that would race across server replicas and couple schema rollout
   to request serving.

5. **Liveness only in M2.1.** Health/readiness endpoints are M2.3. M2.1 uses a minimal
   liveness: a TCP connect to the server port and process supervision for
   worker/reconciler. The richer readiness probe lands in M2.3 against this same
   deployment.

6. **Compose reference.** Extend the ADR-0035 backend compose with the `migrate` one-shot
   and the three app services, wired purely through `KDIVE_*`. The backend services stay.

7. **Helm chart.** `deploy/helm/kdive` deploys the three processes (Deployments + a server
   Service) with config/secret wiring via ConfigMap/Secret against operator-provided
   backends, and a pre-install migrate Job. An off-by-default `bundledBackends` toggle pulls
   Postgres/MinIO subchart dependencies for a turnkey demo.

8. **Publish to GHCR via tagged release CI.** A release workflow builds and pushes to
   `ghcr.io/randomparity/kdive`, tagged by SemVer (ADR-0041) and pinned by digest. A PR job
   builds without pushing so the Dockerfile is exercised on every change. Workflows pin
   actions to SHA with version comments, set `persist-credentials: false`, and are scanned
   with `zizmor`.

9. **Retire the hand-rolled app bootstrap** (replace-don't-deprecate). Remove the `stack`
   subcommand and `run_stack` supervisor, and the `install-compose` / `print-local-env`
   helpers. Retain `migrate`, `install-fixtures`, `seed-demo`, and the live-stack
   **backend** scripts (the M2/M2.5 operator-runs depend on them to bring the backends up).

## Alternatives considered

- **Per-process images** (a light server image, a heavy worker image). Rejected: the band
  design fixes "one image, three entrypoints"; a single image is simpler to publish and
  version, and the toolchain weight is acceptable for a self-hosted platform.
- **Host-libvirt-mountable image** (drive local-libvirt by mounting the host libvirt socket
  with privilege). Rejected for M2.1: it pulls container-privilege and sandboxing concerns
  into the band that the band explicitly defers to M3.
- **Migrate on server startup.** Rejected: races across replicas and couples schema rollout
  to serving (decision 4).
- **Build-only in CI, publish later.** Rejected: the exit criterion is "starts from the
  *published* image"; a publish path is in scope, not deferred.

## Consequences

- The image is the release artifact the band gate consumes (an operator other than the
  author stands kdive up from it). M2.2–M2.4 are developed against the service and run in
  this image but do not depend on its build order.
- The Helm `bundledBackends` toggle keeps stateful-backend operability (PVCs, backups) out
  of the production path while still giving a one-command demo.
- The liveness/readiness split is an explicit hand-off to M2.3: M2.1 ships liveness, M2.3
  adds readiness/health against the same deployment.
- Removing `stack`/`run_stack` changes the local dev story: developers run the image (or the
  compose app tier) rather than the supervisor. The backend bring-up path is unchanged.
