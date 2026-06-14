# M2.x — Productionization & operability band

**Status:** accepted (roadmap addition) · **Date:** 2026-06-10

## Context

The M1.x band deepened features on a single provider before provider expansion;
M2 (remote libvirt) then validated the falsifiable design hypothesis — a second
provider behind the same interfaces, with zero core-surface touches (the committed
`docs/reports/m2-portability.md` gate).

Driving M2 end-to-end on real hardware exposed a different gap. It is not *more
providers* — it is that kdive is not yet operable by anyone but its author.
Standing the service up required hand-rolled bootstrap scripts, direct `psql`/`mc`
poking to read and manage state, an unscripted base-image build, hand-staged
fixture catalogs, and hours lost to *undiagnosed* environment faults (a provider
TLS chain, the gdbstub-port ACL, and a guest→object-store egress path silently
dropped by an unrelated `FORWARD` policy). A build that reported success also
shipped a kernel whose patch had been silently dropped.

These are day-2 platform gaps, and they are exactly the gaps that become dangerous
at M3 (cloud), where real cost, real tenants, and a real secret backend raise the
stakes. So a productionization band gates the cloud expansion.

## Decision

Add **M2.1–M2.4**, a provider-agnostic *productionization & operability band*,
between M2 and M3. Unlike the M1.x band (local-libvirt feature-deepening), these
target the platform itself, not a provider. M2.2–M2.4 act on the **service**, not
the container — they run against a venv deployment exactly as they will against the
M2.1 image — so M2.1 may proceed in parallel; the band's stated order is a delivery
preference, not a hard technical dependency. The order:

- **M2.1 — Deployment & packaging.** Official container image(s) for the three
  processes (one image, entrypoints matching `python -m kdive
  {server|worker|reconciler}`); a reference compose + Helm deployment that brings
  the app tier up against the existing Postgres/MinIO/OIDC backends; and one
  documented configuration surface (the `KDIVE_*` env contract) with a generated
  config reference. Replaces the hand-rolled bootstrap; the image is the release
  artifact the band targets for CI/deployment (but is not a prerequisite for
  developing M2.2–M2.4 — see above).

- **M2.2 — Admin CLI (`kdivectl`).** A supported administrative surface over
  platform state for operators (`platform_admin` / `platform_operator`), not
  agents. It uses the same `kdive.services` / DB seams the MCP tools use (no second
  source of truth) and ships in two scopes:
  - **read-only inspection** (lands first, low risk): resources, allocations,
    systems, runs, jobs, the accounting ledger, object-store wiring, the
    rootfs/fixture catalog, and secret *presence* (never values);
  - **mutating / destructive administration**: cross-project teardown,
    force-release, cordon/drain. These route through the **M1.3 platform-role
    break-glass path** (`mcp/tools/ops/breakglass.py`, `services/allocation/
    release.py`), **not** the per-allocation iteration gate
    (`security/authz/gate.py`), which is allocation- and profile-scoped for an
    agent iterating, not an operator administering.

  **Authentication is the same boundary as the MCP surface, not a bypass.**
  `kdivectl` authenticates as an **OIDC principal** and its actions are subject to
  the identical per-project/platform-role RBAC the MCP transport enforces — "same
  service layer" must not become "same DB credentials, no token." It calls the
  service layer through an authenticated session (the HTTP API or an in-process
  path that still requires and checks a principal token); it does **not** run with
  raw database credentials that would let anyone with host access read state,
  resolve secret presence, or break-glass. Every `kdivectl` action is attributed in
  the audit log under `(principal, operator-cli)`, exactly as MCP tools are.
  Replaces ad-hoc `psql`/`mc` poking — and, unlike that direct DB access, is
  authz-gated and audited.

- **M2.3 — Observability & doctor.** Operational visibility and self-diagnosis:
  structured-log / metrics / trace emission across core and worker, health and
  readiness endpoints for the M2.1 deployment, and a `doctor` / preflight
  (`kdivectl` verb) that validates the contracts whose silent violation cost the
  most in M2 — provider TLS chain, gdbstub-port ACL, secret-ref resolution, and
  guest→object-store reachability — and names the exact fix, rather than
  surfacing as a downstream job failure. **Sequenced here — ahead of the image
  lifecycle — deliberately:** the reachability `doctor` is the band's
  highest-payoff piece (the M2 faults that cost the most were undiagnosed
  reachability), it depends only on the CLI (M2.2) that delivers it, and its checks
  do not need the image subsystem. Its only earlier-feasible slot is folded into
  M2.2; keeping it a distinct milestone right after keeps the CLI scope bounded.
  The metrics/tracing/health work targets the M2.1 deployment, which is settled by
  now.

- **M2.4 — Image & rootfs lifecycle.** A managed subsystem for the base-OS/rootfs
  images that are currently an unscripted "operator obligation": build (the
  per-provider image scripts become first-class and reproducible), validate (the
  guest carries its provider's contract — guest agent, kdump, drgn, the
  allowlisted in-guest helpers), publish/version into the object store, and
  register into the `FixtureCatalog`. M2.2 ships the general admin verbs; the
  **image-management verbs are added here**, against this subsystem (so the two
  are co-designed, not strictly ordered).
  - **Patch-applied verification, not just provenance.** The build check verifies
    the patch produced the expected **source change** (a post-apply diff/marker
    assertion), not merely that the artifact is reproducible from recorded inputs —
    an input→output provenance hash would happily certify a build where the patch
    was a silent no-op. The narrower silent `git apply` patch-drop (#227) is a
    confirmed shipped bug, fixed **independently of and before** this band's exit
    gate (the gate relies on patch-applied verification, so #227's fix is a
    prerequisite of the gate, not band scope).
  - **Publish/register is a two-write with a defined recovery path.** Publish the
    object first, then register the catalog row; a catalog entry is only visible
    once the object's HEAD succeeds, and the reconciler sweeps an object with no
    catalog row (leaked storage) and a catalog row whose object is missing
    (dangling) — the same drift-repair pattern the platform already uses for
    artifacts/Systems, not a new bespoke one.

## Exit criteria

### Per-milestone (so a shortfall surfaces at the milestone, not only at the band gate)

- **M2.1** — the three processes start from the published image with only the
  documented `KDIVE_*` config (no source-tree scripts); the compose/Helm reference
  brings the app tier up healthy against the backends.
- **M2.2** — `kdivectl` lists/inspects every domain object and reports secret
  *presence* under an authenticated principal; an unauthenticated or
  under-privileged invocation is **denied and audited** (the authz boundary is
  proven, not assumed — finding for the whole CLI).
- **M2.3** — `doctor` flags each seeded fault (broken TLS chain, closed gdb ACL,
  missing secret ref, blocked guest→object-store egress) with the exact fix; health
  endpoints report not-ready when a backend is down.
- **M2.4** — a build whose patch is a no-op **fails** patch-applied verification
  (the test asserts the failure, closing the #227 class), and a half-published
  image is reconciled rather than leaving a dangling catalog row.

### Band gate (the M3-entry signal)

"Operable by someone other than its author" needs a falsifiable test, or the gate
is decorative. The band is complete — and M3 may begin — when **an operator who is
not the author stands up kdive on a fresh two-host setup from the published M2.1
image + config reference alone, registers a resource and inspects state via
`kdivectl`, builds a kernel whose patch is confirmed applied, and runs `doctor`** —
recorded operator-run, the shape of the M1.2 / M2 live-stack runbooks.

The record is **independently checkable, not "doctor says green"**: it must carry
the raw evidence — each `doctor` probe's individual result, the patch-applied diff
marker, and a successful end-to-end debug op — so a blind spot in `doctor` (it is
built in this same band; it cannot be its own sole oracle) cannot pass the gate. A
**platform operator other than the author signs off** on the record. M2.1's image
is a prerequisite of this gate even though M2.1 may be *developed* in parallel
(the gate consumes the artifact, not the build order). Until that signed record
exists, the band is not done regardless of how many sub-milestones are merged.

## Non-goals (fold into M3, not standalone M2.x milestones)

- **Hard per-tenant worker sandboxing** (core decision #8, "designed-for but
  deferred") folds into M3 as cloud-driven isolation hardening.
- **A manager-backed secret backend** (Vault / cloud KMS; an open follow-up
  beyond M0 file-refs) folds into M3, where cloud credentials make it
  load-bearing.

## Consequences

- M3 (cloud), M4 (bare metal), and M5 (PowerVM/ppc64le) are **not renumbered**;
  the band inserts as M2.1–M2.4, parallel to how the M1.x milestones sit under M1.
- Each milestone gets its own spec → plan → implementation cycle, and its spec
  names a single accountable owner.
- The band does not alter the provider seam or the MCP tool surface; `kdivectl`
  is an operator surface alongside the agent-facing MCP tools, built on the same
  service layer.
