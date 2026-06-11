# M2.4 — Image & rootfs lifecycle

- **Status:** accepted (design)
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Milestone:** M2.4 (productionization & operability band, see
  [`2026-06-10-m2x-productionization-band-design.md`](2026-06-10-m2x-productionization-band-design.md))
- **ADRs:** [ADR-0092](../../adr/0092-image-rootfs-lifecycle.md) (subsystem, build-plane port,
  DB catalog as single source of truth), [ADR-0093](../../adr/0093-private-image-uploads.md)
  (owner-scoped private uploads, TTL, reconciler auto-prune)

## Context

The M2.x band makes kdive operable by someone other than its author. M2.1 packaged the
service, M2.2 gave operators a CLI, M2.3 gave them observability and a `doctor` preflight.
The remaining day-2 gap is the base-OS/rootfs images.

Today those images are an unscripted operator obligation. Three bash scripts under
`scripts/live-vm/` (`build-guest-image.sh`, `build-busybox-rootfs.sh`,
`fetch-fedora-cloud-image.sh`) build a local-libvirt rootfs by hand; the remote-libvirt
provisioning disk-image (ADR-0080) rides a **placeholder digest**; and the rootfs catalog
(`provider_components/catalog.py`) is **read-only YAML in the source tree**, loaded
synchronously, with no publish, versioning, or drift repair. A separate process cannot
reconcile object-store state against a YAML file on an operator's disk, so there is no way to
detect a half-published image or storage leak.

M2.4 turns image build, validation, publish/version, and registration into a managed
subsystem, and adds a second lifecycle the current catalog cannot express: a user uploading a
**private** image for targeted testing, scoped to that user and pruned on a lifetime.

## Decision

Add `kdive.images`, a provider-agnostic subsystem with per-provider build planes, a
DB-backed catalog that is the single source of truth, two ingestion paths, and reconciler
drift repair. The agent-facing MCP tool surface is unchanged; image management is an operator
surface (and a private-upload surface for authors), built on the same service layer.

### Two ingestion paths

```
OPERATOR path (public base images)          USER path (private targeted images)
  kdivectl images build/publish               kdivectl images upload
        | enqueues IMAGE_BUILD job                  | presigned PUT (reuses ADR-0048 ingest)
        v                                           v
  worker -> RootfsBuildPlane (Python)         validate provider-contract + size cap + quarantine
        |  reproducible, provenance-recorded         |
        v                                           v
  validate provider-contract --------------> publish object --HEAD ok--> register image_catalog row
        v                                                                  (owner, visibility, expires_at)
  reconciler sweeps every pass:
     leaked_images          (object, no row)               -> delete object
     dangling_images        (row, no object)               -> remove row
     expired_private_images (private, expires_at < now())  -> delete object + row  [system:reconciler]
```

### Build planes (Python rewrite)

`images/planes/base.py` defines a `RootfsBuildPlane` port: `build(spec) -> RootfsBuildOutput`
(an object-ready qcow2 plus recorded provenance). Two implementations replace the bash
scripts, which are **deleted** (replace, don't deprecate):

- **`planes/local_libvirt.py`** orchestrates the same unprivileged libguestfs stages the
  scripts do today — `virt-builder` customize, then `virt-tar-out` / `virt-make-fs` repack to
  a no-partition-table whole-disk ext4 qcow2, fstab normalized to a lone `/`, crypttab
  removed, guest-internal SELinux disabled (ADR-0030, ADR-0052) — but in-process, with
  **pinned inputs** (releasever, package set, source-image digest) recorded into `provenance`.
- **`planes/remote_libvirt.py`** produces the provisioning disk-image (ADR-0080), replacing
  the placeholder digest with a real built-and-published image.

The falsifiable contract is **recorded provenance** — the row names exactly the pinned inputs
that produced its image. **Bit-reproducible rebuilds are an explicit non-goal** (a
`virt-builder` image is not bit-stable: mirror drift, embedded timestamps, filesystem
ordering), so an image's identity is a **content digest of the qcow2**, distinct from a kernel
`build_id` (a vmlinux ELF-note that does not exist for a rootfs image). The local plane is
exercised on the **live-stack path**, not stubbed to pass CI — the functional capability the
live-stack runbook and integration tests depend on must survive the rewrite.

### Catalog as single source of truth

Migration `0023_image_catalog.sql` creates `image_catalog`:

| column | notes |
|--------|-------|
| `provider, name, arch, format, root_device` | identity + boot layout |
| `object_key` | object-store key of the qcow2 |
| `build_id` / `digest` | provenance identity |
| `capabilities` | guest contract tags (agent, kdump, drgn, helpers) |
| `provenance` (jsonb) | pinned inputs + build args |
| `visibility` (`public` \| `private`) | resolution scope |
| `owner` (nullable) | the **owning project** (not a principal); set iff `private` |
| `expires_at` (nullable) | required iff `private` |
| `state` | `pending` (row registered, object not yet HEAD-confirmed) → `registered` (resolvable) |
| `pending_since` | timestamp a `pending` row was created; backs the publish-deadline grace window |

DB-level invariants: `CHECK ((visibility='private') = (owner IS NOT NULL))`,
`CHECK ((visibility='private') = (expires_at IS NOT NULL))`, a **partial** unique index on
`(provider, name, arch)` over **`state='registered'` public rows only** (so a crashed publish's
leftover `pending` row never blocks a re-publish of the same identity), and a partial unique
index on `(owner, provider, name)` over registered **private** rows (so a project's private
image name resolves to exactly one image).

An **application-level seed step** (not the SQL migration — `db/migrate.py` applies only
`NNNN_*.sql` and cannot read fixture YAML) reads the operator-configured catalog
(`FIXTURE_CATALOG_PATH`, not just the source-tree default) and registers each entry; it is
read-only against operator data. The in-repo `fixtures/local-libvirt/` default catalog is then
removed from the source tree. Deploy ordering is migrate → seed → resolver cutover, so an old
sync-YAML worker never reads a deleted catalog. The synchronous `load_fixture_catalog()` is replaced by
an `IMAGE_CATALOG` repository read in the provisioning / `materialize` path; resolution moves
to async DB reads. The visibility filter becomes
`visibility='public' OR (visibility='private' AND owner=:project)` — one authz predicate on
the existing visibility seam, not a new mechanism. A project's private image **shadows** a
public image of the same `(provider, name)` (private-first), so resolution is deterministic.
This is the contained hot-path change: the
resolver and its two callers (profile resolution, local_libvirt materialize).

### Publish/register (two-write with recovery)

`services/images/publish.py` registers the row first in `state='pending'`, writes the object,
gates on `store.head()`, then flips the row to `registered`; resolution only ever returns
`registered` rows. Registering the row **before** the object makes a rowless object impossible
during a live publish, closing the window in which `leaked_images` could race the write (see
ADR-0092 §3). A re-run after a crashed attempt adopts the existing `pending` row and re-arms
its `pending_since` rather than colliding — publish is idempotent on its identity. The recovery
path is the reconciler, not a bespoke rollback.

### Reconciler drift repair

Three `_RepairSpec`s appended to `reconciler/loop.py`'s `_repair_plan`, each isolated on a
fresh pooled connection, each evaluating time in Postgres `now()`, modeled on the existing
`_repair_abandoned_uploads` (whose `deadline < now()` window + table cross-check this follows,
not an eager delete). The publish deadline is `pending_since + <publish-grace>` (a `KDIVE_*`
config value); `leaked_images` keys an orphan object's grace off the object's store mtime:

- `leaked_images` — an object under the image prefix with **no row at all**, past the publish
  grace deadline (a build that wrote an object before any `pending` row, or a manual orphan):
  delete the object. A `pending` row inside its deadline protects its object.
- `dangling_images` — a row whose object HEAD is missing **past its publish deadline** (a
  `registered` row whose object was lost, or a `pending` row whose publish crashed before the
  object landed): remove the row. A `pending` row inside its deadline is left alone.
- `expired_private_images` — `visibility='private' AND expires_at < now()`: delete object and
  row, audited under `system:reconciler`, but **reference-guarded** (skip an image a
  non-terminal System/run still uses — a JSONB-containment check against
  `systems.provisioning_profile` — deferring its expiry) and **extend-fenced** (re-read
  `expires_at` under a per-row lock, the ADR-0036 renew analogue). This is the "periodic
  operator pruning" of private uploads, automated like every other TTL on the platform (expired
  allocations, idempotency GC) — self-healing, no standing operator chore. See ADR-0093.

`ReconcileReport` gains three counts and the `loop.py` module docstring's repair list is
updated.

### Private uploads

A project member uploads a private image through the ADR-0048 presigned-PUT ingest (which
supplies the channel, size cap, and quarantine — it validates kernel artifacts, so the rootfs
**guest-contract check is new**: a libguestfs inspection confirms the agent/kdump/drgn/helper
contract while the object is still quarantined). Registration follows ADR-0092's row-first
ordering (pending row → promote object → `registered`) and records `owner=<owning project>`
with a required `expires_at`. Visibility is **project-private** (usable by the owning project,
audited to the uploading principal). Upload admission enforces a per-project quota (count +
total bytes) under the project lock. The reconciler auto-prunes on expiry, but **reference-
guards** (an image a non-terminal System/run still uses is not pruned — its expiry defers) and
**extend-fences** (re-reads `expires_at` under a per-row lock, the ADR-0036 renew analogue).
Operators can prune-early or extend a lifetime; a member can delete an unreferenced one anytime.
See [ADR-0093](../../adr/0093-private-image-uploads.md).

### Verbs + RBAC

A new `kdivectl images` group; the service seams are shared with the worker/job (no second
source of truth), authenticated as an OIDC principal, audited under `(principal, operator-cli)`.

| Verb | Actor | Authz | Action |
|------|-------|-------|--------|
| `images list` | user / operator | RBAC-filtered | public rows + caller's own private rows |
| `images upload` | user (project member) | per-project | presigned PUT, validate, register private row with `expires_at` |
| `images delete <id>` | owner / operator | owner or platform-role | delete own private image (operator: any) |
| `images build` | operator | `platform_operator` | enqueue `IMAGE_BUILD` job for a public base image |
| `images publish` | operator | `platform_operator` | promote a built image to a public catalog row |
| `images prune --expired` / `images extend <id>` | operator | `platform_admin` break-glass | force-prune now / extend a private lifetime |

Mutating/operator verbs route through the M1.3 platform-role **break-glass path**
(`mcp/tools/ops/breakglass.py`), not the per-allocation iteration gate (`security/authz/
gate.py`) — the same boundary M2.2's destructive verbs use. An unprivileged or cross-owner
invocation is **denied and audited** (the authz boundary is proven, mirroring the M2.2 CLI
exit criterion).

### Patch-applied verification

The #227 class (a silent `git apply` no-op shipping an unpatched kernel) is already closed in
**both** build planes via `providers/build_validation.py` (`patch_target_paths` +
`snapshot_file_bytes`, the before/after snapshot plus the `Skipped patch` stderr guard).
M2.4's contribution is the **exit-criterion regression test** that asserts a no-op patch
fails, for both planes — closing the class with a test, not re-implementing the fix. #227's
fix is a merged prerequisite, not band scope.

## Exit criteria

1. A build whose patch is a no-op **fails** patch-applied verification — a regression test
   asserts the failure for both build planes (closes the #227 class).
2. A half-published image (object without row, or row without object) is reconciled rather
   than leaving a dangling/leaked artifact — tested by injecting each half-state.
3. A private upload is visible only within its owning project, and an expired private image is
   auto-pruned by the reconciler — but an expired image a non-terminal System still references
   is **not** pruned (reference guard) — tested with a seeded `expires_at < now()` in both the
   referenced and unreferenced cases.
4. The local-libvirt rootfs build runs through the new Python plane on the live-stack path —
   capability preserved, not stubbed.

## Decomposition

`M2.4/N` issues under an epic, two parallelizable tracks (catalog/data and build/plane):

- **M2.4/1** — `image_catalog` migration `0023` + `IMAGE_CATALOG` repository + seed-and-delete
  YAML + async resolver cutover. *Track head; the catalog table is a dependency of /4–/8.*
- **M2.4/2** — `RootfsBuildPlane` port + local-libvirt Python plane (delete the bash scripts),
  live-stack-exercised.
- **M2.4/3** — remote-libvirt plane: a real built image replacing the placeholder digest
  (ADR-0080).
- **M2.4/4** — publish/register two-write service + `IMAGE_BUILD` job kind + worker handler.
- **M2.4/5** — private upload path (owner-scoped, TTL, provider-contract validation; reuses
  the ADR-0048 ingest seam).
- **M2.4/6** — three reconciler sweeps (`leaked_images` / `dangling_images` /
  `expired_private_images`) + `ReconcileReport` counts.
- **M2.4/7** — `kdivectl images` verbs + RBAC / break-glass wiring + audit attribution.
- **M2.4/8** — exit-criterion proof tests (patch no-op fails; half-publish reconciled; private
  isolation + auto-prune) + operator runbook.

## Non-goals

- The agent-facing MCP tool surface is not extended; image management is an operator surface
  and a private-upload surface, both on the CLI/HTTP boundary.
- No dual catalog backing: the YAML files are removed, not kept as a fallback. The DB table is
  the single source of truth.
- Tier-3 in-guest kdump image work (#115) and >5 GiB uploads (#112) remain their own issues.

## Consequences

- Resolution in the provisioning/`materialize` path becomes an async DB read; the synchronous
  YAML loader is removed along with the `fixtures/` source-tree catalog.
- The reconciler gains three sweeps and three report counts; its pass cost grows by the
  object-prefix and per-row HEAD checks those sweeps require.
- The provider build seam gains a `RootfsBuildPlane`; every new provider that ships a base
  image implements it (as the systems registrar already requires a `rootfs_validator`).
- The M2.4 band gate consumes the published public base image (operator-run, signed by a
  non-author operator), so the remote plane's real image closes a known M3-entry gap.
