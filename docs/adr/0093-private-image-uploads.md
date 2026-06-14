# ADR 0093 — Private image uploads: owner-scoped, TTL'd, reconciler-pruned (M2.4)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0092](0092-image-rootfs-lifecycle.md) (the
  `image_catalog` table and publish/register path this scopes by owner),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the presigned-PUT ingest, size cap,
  and quarantine the upload reuses), [ADR-0021](0021-reconciler-loop-drift-repair.md) (the
  drift-repair loop the prune sweep extends), and [ADR-0006](0006-oidc-rbac-attribution.md)
  (the project an upload is owned by and the `(principal, operator-cli)` audit attribution).
- **Spec:** [`../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`](../archive/superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md)
- **Milestone:** M2.4

## Context

ADR-0092 manages operator-published **public** base images. A project also needs to test
against its own image — a one-off rootfs or a modified base — without publishing it
platform-wide or asking an operator to register it. Such an image must be visible only within
its owning project, and it must not accumulate forever: scratch images with no lifetime and no
cap become unbounded object-store cost and an operator cleanup chore.

The platform already has most of the primitives. External-build ingestion (ADR-0048) supplies
the presigned-PUT channel, the size cap, and quarantine for a user upload (it validates kernel
artifacts, not a rootfs — the guest-contract check is new here). The reconciler already
auto-reaps on a Postgres `now()` predicate (expired allocations, idempotency GC). The catalog
already filters on a `visibility` seam, and projects already carry a `quotas` row.

## Decision

We will add a **private** image lifecycle to `image_catalog`, scoped to the owning project and
pruned by the reconciler on a lifetime.

1. **Owner scoping is project-private, on the existing visibility seam.** A private row carries
   `visibility='private'`, `owner=<owning project>` (the project the upload was made under,
   matching the platform's project-scoped RBAC and `max_concurrent_systems` quota — not an
   individual principal, so a private image is usable by the project that paid for it, not
   stranded on one user), and a required `expires_at`. The uploading principal is still recorded
   for audit (ADR-0006), but the *visibility* boundary is the project. DB `CHECK` constraints
   tie `owner` and `expires_at` to `private`. Resolution becomes `visibility='public' OR
   (visibility='private' AND owner=:project)` — one authz predicate on the existing seam, not a
   new mechanism. A private image is never visible or usable outside its owning project. When a
   project's private image shares `(provider, name)` with a public one, the **private image
   shadows the public** (private-first resolution within the owning project), so the result is
   deterministic — a project testing a modified base under the same name gets its own image, and
   other projects still see the public one.
2. **Upload reuses the ADR-0048 ingest, then runs a new guest-contract validator.** A project
   member uploads via presigned PUT; ADR-0048 provides the presigned channel, the size cap, and
   quarantine. The **guest-contract check is new to this ADR** (ADR-0048 validates kernel
   artifacts — ELF/bzImage/build-id — not a rootfs): a libguestfs inspection of the qcow2
   confirms the provider contract (guest agent, kdump, drgn, allowlisted helpers) while the
   object is still in the quarantine prefix. A contract failure rejects the upload with a named
   reason, not a downstream provision error. Registration then follows **ADR-0092's row-first
   ordering** to stay clear of the `leaked_images` race: write the `pending` row, promote the
   validated object from quarantine into the swept image prefix, then flip to `registered` — so
   an object in the swept prefix always has a protecting row. A private row's resolution key is
   unique: `(owner, provider, name)` over registered private rows, so a project's private image
   name resolves to exactly one image.
3. **A per-owner quota bounds aggregate footprint; the TTL bounds lifetime.** The required
   `expires_at` bounds how long one image lives, but not how many a project holds. Upload
   admission enforces a per-project cap on concurrent private images (a count and a total-bytes
   limit, fail-closed like the `max_concurrent_systems` quota) under the held project lock, so
   two concurrent uploads cannot both pass the cap check. Aggregate private storage is bounded,
   not just time-limited.
4. **The reconciler auto-prunes on expiry, fenced and reference-guarded.** A
   `expired_private_images` sweep deletes the object and row of an expired `private` image,
   audited under `system:reconciler` — the same self-healing TTL the platform uses everywhere
   else. Two guards make the delete safe:
   - **Reference guard.** An image still referenced by a **non-terminal System or run** is not
     pruned; its expiry defers until the last dependent reaches a terminal state (mirroring "a
     System never outlives its Allocation"). A TTL expiry never removes a backing object out
     from under a provisioned or reprovisioning System. The reference is the rootfs ComponentRef
     inside `systems.provisioning_profile` (JSONB) — there is no FK column — so the guard is a
     JSONB-containment query matching the private image's identity against non-terminal Systems,
     not a join. A still-referenced expired image keeps its quota slot until it is unreferenced.
     Operators see the deferred-prune state.
   - **Extend fence.** The sweep re-validates `expires_at < now()` under a per-row lock before
     deleting, so an `extend` (or `delete`) that committed between candidate-selection and the
     delete is honored and the image is not clobbered — the ADR-0036 renew-vs-expiry fence,
     applied to image TTLs.

   Operators can prune-early or extend a lifetime; users can delete their own (unreferenced)
   image at any time. There is no standing operator cleanup chore.

## Consequences

- `image_catalog` gains `owner`, `visibility`, and `expires_at` columns with `CHECK`
  constraints binding them to the private case, plus a partial unique index on
  `(owner, provider, name)` over registered private rows (defined in the ADR-0092
  migration `0023`).
- A **new guest-contract validator** (libguestfs inspection of the uploaded qcow2) is added —
  it is not part of ADR-0048's kernel-artifact validation and runs while the upload is still
  quarantined.
- A **per-project private-image quota** (count and total bytes) is enforced at upload
  admission, fail-closed, alongside the existing `quotas` row.
- The reconciler gains a third sweep and a `ReconcileReport` count; the sweep carries a
  reference guard (skips images a non-terminal System/run still uses, leaving a
  deferred-prune state) and an extend fence (re-read under a per-row lock).
- `kdivectl images` gains `upload` / `delete` (owner) and `prune` / `extend` (operator) verbs;
  a cross-project or unprivileged invocation is denied and audited.
- The default lifetime, the maximum extendable lifetime, and the per-project private-image
  count/bytes caps become `KDIVE_*` config values with generated-reference entries.

## Alternatives considered

- **A separate `private_images` table.** Cleaner row-type separation, but resolution would
  union two tables and the reconciler would sweep two backings; the visibility column already
  expresses the distinction in one table. Rejected for the single-table model.
- **No TTL; operator deletes private images manually.** Matches a literal reading of "operator
  pruning", but it is the only non-self-healing TTL on the platform and lets private storage
  grow until someone runs the chore. Rejected for reconciler auto-prune with operator
  override.
- **Expose private upload as an agent-facing MCP tool.** An author is often an agent, so this
  is tempting, but image management is an operator/author surface on the CLI/HTTP boundary and
  the band's non-goal keeps the agent MCP surface unchanged. Rejected; the upload stays on the
  CLI/HTTP boundary.
