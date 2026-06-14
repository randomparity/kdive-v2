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
subsystem, and adds a second lifecycle the current catalog cannot express: a project member
uploading a **private** image for targeted testing, scoped to the owning project and pruned on
a lifetime.

## Decision

Add `kdive.images`, a provider-agnostic subsystem with per-provider build planes, a
DB-backed catalog that is the single source of truth, two ingestion paths, and reconciler
drift repair. The agent-facing MCP tool surface is unchanged; image management is an operator
surface (and a private-upload surface for authors), built on the same service layer.

### Two ingestion paths

```
OPERATOR path (public base images)          USER path (private targeted images)
  kdivectl images build/publish               kdivectl images upload
        | enqueues IMAGE_BUILD job                  | presigned PUT to quarantine (ADR-0048)
        v                                           v
  worker -> RootfsBuildPlane (Python)         size cap + quarantine, then NEW guest-contract
        |  provenance-recorded                       |  validation (libguestfs inspect)
        v                                           v
  guest-contract validation ----------------> register image_catalog row (state=pending)
                                                     |  (owner=project iff private, expires_at)
                                                     v
                                              write/promote object --HEAD ok--> flip row to registered
                                                  (row-first; a swept-prefix object always has a row)

  reconciler sweeps every pass (deadline-guarded, not eager):
     leaked_images          (object, no row, past publish grace)    -> delete object
     dangling_images        (row, object missing past deadline)     -> remove row
     expired_private_images (private, expires_at < now())           -> delete object + row
                              ...skipped if a non-terminal System still references it  [system:reconciler]
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
exercised on the **operator-run live-stack path** (env-gated on `KDIVE_LIVE_SSH_TARGET`, like
the band gate — not normal CI, which skips it); a CI smoke may cover plane wiring, but the
capability proof is operator-run. The functional capability the live-stack runbook and
integration tests depend on must survive the rewrite — the plane is not stubbed to a no-op.

### Catalog as single source of truth

Migration `0023_image_catalog.sql` creates `image_catalog`:

| column | notes |
|--------|-------|
| `provider, name, arch, format, root_device` | identity + boot layout |
| `object_key` (nullable) | object-store key of the qcow2; **NULL for a `defined` row** (metadata seeded, no image built yet) |
| `digest` | content digest of the qcow2 — the image identity (a rootfs image has no kernel `build_id`); NULL until built |
| `capabilities` | guest contract tags (agent, kdump, drgn, helpers) |
| `provenance` (jsonb) | pinned inputs + build args |
| `visibility` (`public` \| `private`) | resolution scope |
| `owner` (nullable) | the **owning project** (not a principal); set iff `private` |
| `expires_at` (nullable) | required iff `private` |
| `state` | `defined` (seeded metadata, no object) → `pending` (publishing) → `registered` (resolvable) |
| `pending_since` | timestamp a `pending` row was created; backs the publish-deadline grace window |

DB-level invariants: `CHECK ((visibility='private') = (owner IS NOT NULL))`,
`CHECK ((visibility='private') = (expires_at IS NOT NULL))`, `CHECK ((state='defined') =
(object_key IS NULL))` (a `defined` baseline has no object; `pending`/`registered` do), a
**partial** unique index on `(provider, name, arch)` over **`state='registered'` public rows
only** (so a crashed publish's leftover `pending` row never blocks a re-publish of the same
identity), the same over `state='defined'` public rows (seed idempotency), and a partial unique
index on `(owner, provider, name)` over registered **private** rows (so a project's private
image name resolves to exactly one image).

A **`defined`** row is why a fresh install works: baseline rootfs qcow2 bytes (~GiB) cannot ship
in the package, so the seed registers baseline *metadata* as `defined` rows and `images
build`/`publish` realizes each to `registered` (`defined → pending → registered`). Resolution
returns only `registered` rows, so a `defined`-only baseline is listed but not yet bootable.

An **application-level seed step** (not the SQL migration — `db/migrate.py` applies only
`NNNN_*.sql` and cannot read fixture YAML) registers the baseline **rootfs** as `defined` rows
from the operator-configured catalog (`FIXTURE_CATALOG_PATH`) or, on a fresh install, the
packaged baseline relocated into `images/seed_data/`; it is read-only against operator data.

**Only the rootfs catalog moves to the DB.** The fixture catalog also holds `profiles`
(`ProfileCatalogEntry`, cmdline/config requirements) that `image_catalog` does not model — so
only `fixtures/local-libvirt/rootfs/` is relocated to `images/seed_data/`; `profiles/` and the
configs **stay file-based** and `load_fixture_catalog` keeps resolving them (its
`FixtureCatalog.rootfs` list becomes empty, `.profiles` unchanged).

The synchronous rootfs lookup in the provisioning / `materialize` path is replaced by an
`IMAGE_CATALOG` async DB read **plus a wired object fetch**: object-store-backed materialization
is a `not wired yet` stub today (`materialize.py`), so the resolver returns the row and
`materialize` downloads its `object_key` to a checksum-verified local cache for the provider to
boot. The visibility filter becomes `visibility='public' OR (visibility='private' AND
owner=:project)` — one authz predicate on the existing visibility seam, not a new mechanism. A
project's private image **shadows** a public image of the same `(provider, name)` (private-first),
so resolution is deterministic. This is the contained hot-path change: the resolver and its
callers.

### Publish/register (two-write with recovery)

`services/images/publish.py` adopts the identity's existing `defined`/`pending` row (or inserts
a `pending` row), sets its `object_key`, writes the object, gates on `store.head()`, then flips
the row to `registered`; resolution only ever returns `registered` rows. Realizing a seeded
`defined` baseline is this same path (`defined → pending → registered`). Registering the row
**before** the object makes a rowless object impossible
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
- `dangling_images` — operates only on rows with `object_key IS NOT NULL` (a `defined` baseline
  is object-less by design and never dangling): a row whose object HEAD is missing **past its
  publish deadline** (a `registered` row whose object was lost, or a `pending` row whose publish crashed before the
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
| `images list` | project member / operator | RBAC-filtered | public rows + the caller's **project's** private rows |
| `images upload` | project member | per-project | presigned PUT, validate, register a project-private row with `expires_at` |
| `images delete <id>` | project member / operator | project-scoped; operator cross-project via break-glass | delete the project's (unreferenced) private image (operator: any, via break-glass) |
| `images build` | operator | `platform_operator` | enqueue `IMAGE_BUILD` job for a public base image |
| `images publish` | operator | `platform_operator` | promote a built image to a public catalog row |
| `images prune --expired` / `images extend <id>` | operator | `platform_admin` break-glass | force-prune now / extend a private lifetime |

Routine `build`/`publish` authorize as `platform_operator`; only the destructive verbs
(`prune`, `extend`, cross-project force-`delete`) route through the M1.3 platform-role
**break-glass path** (`mcp/tools/ops/breakglass.py`), not the per-allocation iteration gate
(`security/authz/gate.py`) — the same boundary M2.2's destructive verbs use. An unprivileged or
cross-project invocation is **denied and audited** (the authz boundary is proven, mirroring the
M2.2 CLI exit criterion).

### Patch-applied verification (kernel build planes — not the rootfs plane above)

This concerns the **kernel** build planes (`providers/{local,remote}_libvirt/build.py`,
`_apply_patch`), a separate subsystem from the rootfs `RootfsBuildPlane` introduced above — a
rootfs image applies no kernel patch. M2.4 does not rewrite the kernel build planes; it carries
this exit criterion only because the band gate requires it.

The #227 class (a silent `git apply` no-op shipping an unpatched kernel) is already closed in
**both kernel build planes** via `providers/build_validation.py` (`patch_target_paths` +
`snapshot_file_bytes`, the before/after snapshot plus the `Skipped patch` stderr guard).
M2.4's contribution is the **exit-criterion regression test** that asserts a no-op patch
fails, for both kernel planes — closing the class with a test, not re-implementing the fix.
#227's fix is a merged prerequisite, not band scope.

## Exit criteria

1. A kernel build whose patch is a no-op **fails** patch-applied verification — a regression
   test asserts the failure for both kernel build planes (closes the #227 class).
2. A half-published image (object without row, or row without object) is reconciled rather
   than leaving a dangling/leaked artifact — tested by injecting each half-state.
3. A private upload is visible only within its owning project, and an expired private image is
   auto-pruned by the reconciler — but an expired image a non-terminal System still references
   is **not** pruned (reference guard) — tested with a seeded `expires_at < now()` in both the
   referenced and unreferenced cases.
4. A private upload that lacks the guest contract (no agent/kdump/drgn) is **rejected** with a
   named reason, and an upload over the per-project count/bytes cap is **denied** — both audited;
   tested by uploading a non-conforming image and by exceeding the cap.
5. The local-libvirt rootfs build runs through the new Python plane on the **operator-run**
   live-stack path (env-gated, not normal CI) — capability preserved, not stubbed.

## Decomposition

`M2.4/N` issues under an epic. Two tracks run in parallel up front — **catalog/data** (/1)
and **build/plane** (/2, /3, which need no DB table) — then the service/wiring issues chain.

- **M2.4/1** — `image_catalog` migration `0023` + `IMAGE_CATALOG` repository + seed baseline
  rootfs as `defined` rows (relocate `rootfs/` → `images/seed_data/`; profiles stay file-based)
  + async resolver cutover **with the wired object fetch**. *Catalog-track head.* **Migration
  `0023` is authored whole here with the full public + private schema** (owner, expires_at, pending_since, state, both
  partial unique indexes) so no later issue adds a second migration — the single migration owner,
  to avoid the parallel registry conflict the M2.2/M2.3 waves hit.
- **M2.4/2** — `RootfsBuildPlane` port + local-libvirt Python plane, exercised on the
  operator-run live-stack path. *Build-track head; independent of /1.* Removing the bash
  builders is a **migration, not a delete**: rewire the production consumer
  (`providers/local_libvirt/lifecycle/provisioning.py`), the live-stack integration tests
  (`test_live_stack.py`, `conftest.py`) and `tests/scripts/test_live_vm_fixtures.py`, and the
  `docs/runbooks/live-stack.md` operator runbook onto the plane / `kdivectl images build`; the
  scripts are deleted only after every consumer moves.
- **M2.4/3** — remote-libvirt plane: a real built image replacing the placeholder digest
  (ADR-0080). *Independent of /1.*
- **M2.4/4** — publish/register two-write service + `IMAGE_BUILD` job kind + worker handler.
  *After /1.*
- **M2.4/5** — private upload path: per-project quota (count+bytes under the project lock) and a
  **new** guest-contract validator (libguestfs inspection); ADR-0048 supplies transport/cap/
  quarantine only. *After /1 and /4 (reuses the publish/register service).*
- **M2.4/6** — three reconciler sweeps (`leaked_images` / `dangling_images` /
  `expired_private_images`, the last reference-guarded + extend-fenced) + `ReconcileReport`
  counts. *After /1 and /4 (needs the `state`/`pending_since`/`expires_at` columns and the
  publish ordering).*
- **M2.4/7** — `kdivectl images` verbs + RBAC / break-glass wiring + audit attribution.
  *After /4, /5, /6 (wraps their services).*
- **M2.4/8** — exit-criterion proof tests (patch no-op fails; half-publish reconciled; private
  isolation + reference-guarded auto-prune; guest-contract rejection + over-quota denial) +
  operator runbook. *Last.*

## Non-goals

- The agent-facing MCP tool surface is not extended; image management is an operator surface
  and a private-upload surface, both on the CLI/HTTP boundary.
- No dual catalog backing for **rootfs images**: the DB table is the single source of truth and
  the rootfs YAML is relocated into the seed (not kept as a runtime fallback). The *profiles*
  catalog is a separate concern and stays file-based this milestone.
- Tier-3 in-guest kdump image work (#115) and >5 GiB uploads (#112) remain their own issues.
  A private rootfs upload is bounded by the ADR-0048 object-size cap; a qcow2 is normally sparse
  enough to fit, but an image above the cap (e.g. a debug-heavy rootfs with a staged vmlinux)
  needs #112.

## Consequences

- Resolution in the provisioning/`materialize` path becomes an async DB read **plus an
  object-store fetch** (replacing the `not wired yet` stub); the synchronous YAML *rootfs*
  lookup is removed (the rootfs catalog moves to the DB), while `load_fixture_catalog` is kept
  for profiles.
- The reconciler gains three sweeps and three report counts; its pass cost grows by the
  object-prefix listing, the per-row HEAD checks, and the per-pass JSONB scan over non-terminal
  Systems the `expired_private_images` reference guard runs.
- The provider build seam gains a `RootfsBuildPlane`; every new provider that ships a base
  image implements it (as the systems registrar already requires a `rootfs_validator`).
- The M2.4 band gate consumes the published public base image (operator-run, signed by a
  non-author operator), so the remote plane's real image closes a known M3-entry gap.
