# ADR 0088 — Deployment & packaging: one multi-process image, compose + Helm reference (M2.1)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0035](0035-walking-skeleton-e2e-harness.md) (the
  backend-only dev compose this extends with the app tier), [ADR-0041](0041-versioning-release-process.md)
  (SemVer, milestone→minor — the image tag scheme), [ADR-0076](0076-remote-libvirt-provider-package.md)
  (the remote-libvirt provider the image is built to drive), [ADR-0087](0087-config-registry.md)
  (the registry-validated `KDIVE_*` config the container takes as its only input),
  [ADR-0015](0015-sql-migration-runner.md) (the forward-only, locked, idempotent migration
  runner the migrate one-shot invokes).
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

3. **Configuration is the only *config* input; the worker also needs writable volumes.**
   No source-tree scripts in the container. Config is the registry-validated `KDIVE_*` env
   (ADR-0087) plus secret files mounted under `KDIVE_SECRETS_ROOT` — but config is not the
   only *runtime* input. The worker does real I/O and needs writable, correctly-owned
   volumes (it runs non-root, so ownership/`fsGroup` matters): the build workspace
   (`KDIVE_BUILD_WORKSPACE`, default `/var/lib/kdive/build`), install staging
   (`KDIVE_INSTALL_STAGING`, `/var/lib/kdive/install`), the managed SSH key dir
   (`KDIVE_SSH_KEY_DIR`), and the debug/crash transcript dirs (`KDIVE_DEBUG_DIR`,
   `KDIVE_CRASH_DIR`). A read-only or empty-filesystem worker starts but fails the first
   build/install job. The compose and Helm references mount these as named volumes owned by
   the non-root UID; the server and reconciler need none of them.

4. **Migrations run as a dedicated one-shot.** Compose: a `migrate` service the app services
   depend on **with `condition: service_completed_successfully`** — a bare `depends_on` waits
   only for the one-shot to *start*, which would let the apps boot and hit the DB before
   migrations finish; the completion condition (and a non-zero migrate exit blocking app
   start) is what makes the one-shot actually ordering-safe. Helm: a pre-install/pre-upgrade
   Job (this assumes pre-existing external backends; the `bundledBackends` demo path orders
   migration after the bundled DB is ready — see decision 7). Migrations are not on the server
   startup path — that would couple schema rollout to request serving. Concurrency,
   atomicity, and idempotency are **already guaranteed by the runner** (ADR-0015: a database
   lock so two migrators cannot both apply a file, transactional all-or-nothing, no-op on
   re-run), so the one-shot needs no extra coordination.

   **Rolling-upgrade and rollback contract.** A pre-upgrade Job applies the new schema while
   the *previous* server/worker replicas are still serving (a rolling upgrade keeps old pods
   until new ones are ready), and the runner is **forward-only** (ADR-0015) — there are no
   down-migrations. Two consequences this deployment must honor: (a) migrations are
   **backward-compatible / expand-contract** — a new schema must be tolerated by the
   still-running prior code, or the chart must be run with an explicit stop-old-first
   (downtime) window rather than a rolling update; (b) rollback is **image-only** — reverting
   to a prior image leaves the schema rolled forward, so the prior image must also tolerate
   the newer schema. The compose/Helm references default to the rolling path and therefore
   assume (a); a migration that cannot be made backward-compatible is a documented
   downtime-window release, not a silent rolling one.

5. **Liveness only in M2.1, with "healthy" defined to match it.** Rich health/readiness
   endpoints are M2.3. M2.1 ships a minimal liveness: a TCP connect to the server port and
   process supervision for worker/reconciler. A TCP-open server can still be wedged (DB pool
   down), so M2.1's exit-criterion "up healthy" is **defined as the weaker, verifiable
   claim**: `migrate` exited 0, all three processes are running and stay up (no crash-loop),
   and the server accepts connections — plus the ADR-0087 startup validation, which already
   fails a misconfigured process fast *before* it binds, so a process that reaches "listening"
   has at least passed config validation. Proving a live DB round-trip and dependency
   readiness is explicitly M2.3's readiness probe against this same deployment, not an M2.1
   claim.

6. **Compose reference.** Extend the ADR-0035 backend compose with the `migrate` one-shot
   and the three app services, wired purely through `KDIVE_*`. The backend services stay.

7. **Helm chart.** `deploy/helm/kdive` deploys the three processes (Deployments + a server
   Service) with config/secret wiring via ConfigMap/Secret against operator-provided
   backends, and a pre-install migrate Job. An off-by-default `bundledBackends` toggle pulls
   Postgres/MinIO subchart dependencies for a turnkey demo.

   **Bundled backends are ephemeral and demo-only — not a production path.** They back the
   system-of-record on `emptyDir` (no PVC, no backup), so a pod restart drops all state by
   design; the chart treats this as a feature of a throwaway demo, not a gap. To make the
   footgun hard to fire, `bundledBackends: true` requires a co-set `demoAcknowledged: true`
   (the chart fails to render otherwise) and the rendered notes state the data is
   non-durable. Production runs against operator-provided backends with the toggle off.

   **Migrate-Job phase reconciles with bundled backends.** Decision 4's pre-install/
   pre-upgrade migrate Job assumes the backends **pre-exist** — true for the production path,
   where Postgres is operator-provided and already up. A Helm pre-install hook runs *before*
   the release's normal resources (including the bundled Postgres subchart) are created, so
   on the `bundledBackends` demo path a pre-install migrate would race a database that does
   not exist yet. The demo path therefore orders migration *after* the bundled DB is ready —
   a `post-install`/`post-upgrade` hook weighted after the subchart, or a DB-readiness wait on
   the migrate Job — rather than the pre-install phase the external-backend path uses. The
   production path (external backends, pre-install) is unchanged.

8. **Publish to GHCR via tagged release CI.** A release workflow builds and pushes to
   `ghcr.io/randomparity/kdive`, tagged by SemVer (ADR-0041) and pinned by digest. A PR job
   builds without pushing so the Dockerfile is exercised on every change. Workflows pin
   actions to SHA with version comments, set `persist-credentials: false`, and are scanned
   with `zizmor`.

   **Artifact provenance** — cosign signing and an SBOM — is **in M2.1 scope**, because the
   image is the artifact the band gate's non-author operator is asked to trust: the release
   workflow signs the pushed digest (keyless/OIDC cosign) and attaches an SBOM, and the
   band-gate runbook verifies the signature before standing kdive up. Full build
   *reproducibility* (pinned toolchain layers) is the separate, deeper concern that belongs
   to M2.4's image-lifecycle work, not here.

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
- **Migrate on server startup.** Rejected: the runner's lock (ADR-0015) already prevents a
  double-apply race, so the objection is *coupling*, not safety — it ties schema rollout to
  the serving deploy and makes every replica restart attempt a migrate, instead of one
  explicit, observable one-shot (decision 4).
- **Build-only in CI, publish later.** Rejected: the exit criterion is "starts from the
  *published* image"; a publish path is in scope, not deferred.

## Consequences

- The image is the release artifact the band gate consumes (an operator other than the
  author stands kdive up from it). M2.2–M2.4 are developed against the service and run in
  this image but do not depend on its build order.
- The Helm `bundledBackends` toggle keeps stateful-backend operability (PVCs, backups) out
  of the production path while still giving a one-command demo — enforced by the
  `demoAcknowledged` gate and ephemeral `emptyDir` storage (decision 7), not just asserted.
- The liveness/readiness split is an explicit hand-off to M2.3: M2.1 ships liveness, M2.3
  adds readiness/health against the same deployment.
- Removing `stack`/`run_stack` changes the local dev story: developers run the image (or the
  compose app tier) rather than the supervisor. The backend bring-up path is unchanged.
