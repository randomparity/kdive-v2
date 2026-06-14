# ADR 0092 — Image & rootfs lifecycle: managed subsystem, build-plane port, DB catalog (M2.4)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0052](0052-bootable-rootfs-image-builder.md)
  (the unprivileged libguestfs rootfs build the local plane now orchestrates in-process),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the remote provisioning
  disk-image whose placeholder digest this replaces), [ADR-0048](0048-external-build-artifact-ingestion.md)
  (the presigned-PUT ingest + validation the private-upload path reuses), and
  [ADR-0021](0021-reconciler-loop-drift-repair.md) (the periodic drift-repair loop the new sweeps
  extend).
- **Spec:** [`../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`](../archive/superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md)
- **Milestone:** M2.4

## Context

Base-OS/rootfs images are the last day-2 operator obligation in the M2.x band that is still
unscripted. Three bash scripts under `scripts/live-vm/` build a local-libvirt rootfs by hand;
the remote-libvirt provisioning disk-image (ADR-0080) rides a placeholder digest; and the
rootfs catalog is read-only YAML in the source tree, loaded synchronously, with no publish,
versioning, or drift repair. A separate process cannot reconcile object-store state against a
YAML file on an operator's disk, so a half-published image or a storage leak is undetectable.

The band gate is operator-run on real hardware against the published image; an unscripted,
unverifiable image build cannot pass it.

## Decision

We will add `kdive.images`, a provider-agnostic subsystem with per-provider Python build
planes, a DB-backed catalog as the single source of truth, and reconciler drift repair.

1. **A `RootfsBuildPlane` port replaces the bash scripts.** Per-provider implementations
   (local-libvirt orchestrating the libguestfs stages in-process; remote-libvirt building a
   real provisioning disk-image) produce a qcow2 plus **recorded provenance** — the pinned
   input digests and build args captured into the catalog row. Recorded provenance is the
   falsifiable contract (a row names exactly what produced its image). **Bit-reproducible
   rebuilds are an explicit non-goal:** a `virt-builder`-customized image is not bit-stable
   (mirror drift, embedded timestamps, filesystem ordering), so the image's identity is a
   **content digest of the qcow2** (distinct from a kernel `build_id`, which is a vmlinux
   ELF-note and does not exist for a rootfs image). The bash scripts are removed, not kept.
   The local plane is exercised on the live-stack path, not stubbed.
2. **The `image_catalog` Postgres table is the single source of truth.** Migration `0023`
   creates the table (schema only). A separate **application-level seed step** (not the SQL
   migration — `db/migrate.py` applies only `NNNN_*.sql` and cannot read fixture YAML) reads
   the operator-configured catalog (`FIXTURE_CATALOG_PATH`, not just the source-tree default,
   so a customized catalog is not silently dropped) and registers each entry. The seed is
   **read-only against operator data** — it never deletes the files it read. The in-repo
   `fixtures/local-libvirt/` *default* catalog is removed from the source tree (a code change,
   not a runtime action); an operator's runtime catalog stays on their disk untouched.
   **Deploy ordering is migrate → seed → cut the resolver over to DB reads**, so an old
   (sync-YAML) worker never reads a catalog the new code already deleted. Resolution in the
   provisioning/`materialize` path then moves to async DB reads. There is no dual backing once
   the cutover completes.
3. **Publish/register is a two-write with the reconciler as the recovery path.** Register the
   row first in a `pending` state, publish the object, gate on `store.head()`, then flip the
   row to `registered`; resolution only ever returns `registered` rows. Making the **rowless
   object impossible** (the row precedes the object) is deliberate: it removes the window in
   which `leaked_images` could race a live publish. A `pending` row carries a `pending_since`
   timestamp; its publish deadline is `pending_since + <publish-grace>` (a `KDIVE_*` config
   value), and `leaked_images` keys an orphan object's grace off the object's own store mtime.
   Both sweeps evaluate the deadline with Postgres `now()` (never a Python clock), guarded like
   the precedent `repair_abandoned_uploads` (a `deadline < now()` window plus a row
   cross-check), not an eager delete:
   - `dangling_images` — a row whose object HEAD is missing **past its publish deadline**:
     either a `registered` row whose object was later lost, or a `pending` row whose publish
     crashed before the object landed → remove the row. A `pending` row inside its deadline is
     left alone (its publish may still be in flight).
   - `leaked_images` — an object with **no row at all** and past the publish grace deadline →
     delete the object (a build that wrote an object before any `pending` row, or an orphan
     from manual poking).

   The `pending` row and the object are thus mutually protective inside the deadline: the row
   keeps `leaked_images` off the object, and the deadline keeps `dangling_images` off the row.
   The `unique(provider, name, arch)` constraint applies only to `registered` rows, so a
   re-run of `images publish` after a crashed attempt adopts the existing `pending` row and
   re-arms its deadline rather than colliding — publish is idempotent on its identity.

   This is the same drift-repair pattern the platform already uses for uploaded artifacts
   (`reconciler/uploads.py` cross-checks `artifacts` before deleting) and Systems, not a
   bespoke rollback.
4. **Image management stays an operator surface.** Build/publish run as an `IMAGE_BUILD` job an
   operator verb enqueues, processed by the worker; the agent-facing MCP tool surface is not
   extended. Routine operator verbs (`build`/`publish`) authorize as `platform_operator`; only
   the **destructive** verbs (`prune`, `extend`, cross-owner `delete`) route through the M1.3
   `platform_admin` break-glass path — routine creation is not over-escalated through the
   emergency-override path. See the spec's verb table for the per-verb authz.

## Consequences

- The synchronous YAML catalog loader and the `fixtures/` source-tree catalog are removed;
  provisioning resolution becomes an async DB read in the resolver and its two callers.
- The reconciler gains two sweeps (three with ADR-0093's private-image prune) and matching
  `ReconcileReport` counts; per-pass cost grows by the object-prefix and per-row HEAD checks.
- Every new provider that ships a base image implements `RootfsBuildPlane`, as the systems
  registrar already requires a `rootfs_validator`.
- A new `IMAGE_BUILD` job kind, a migration `0023`, and a `services/images/` service layer
  shared by the worker and the `kdivectl images` verbs.
- The remote plane's real image closes a known M3-entry gap (the ADR-0080 placeholder digest).

## Alternatives considered

- **Keep the bash scripts, wrap them in a managed port.** Smaller change, but the scripts stay
  the build mechanism — provenance recording bolts on awkwardly around shell, and two build
  idioms (shell rootfs, Python kernel) persist. Rejected for a uniform Python build seam.
- **Keep the YAML catalog, add a separate DB table for managed images.** Avoids touching the
  hot resolution path, but creates two sources of truth and a union read; "register into the
  catalog" with reconciler row-sweeps then means a second catalog. Rejected per replace-don't-
  deprecate: one DB table, YAML seeded in and removed.
- **Model image build as a per-System run.** Reuses the runs lifecycle, but base-image build is
  operator infra, not an agent-driven per-System action; it would put operator infra on the
  agent MCP surface. Rejected for an `IMAGE_BUILD` job on the existing worker tier.
