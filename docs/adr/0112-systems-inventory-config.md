# ADR 0112 — Declarative systems inventory config (`systems.toml`)

- **Status:** Proposed
- **Date:** 2026-06-14
- **Depends on:** [ADR-0087](0087-config-registry.md) (the `KDIVE_*` operational-config
  registry — this ADR draws the boundary: env owns operational/secret runtime config,
  `systems.toml` owns declared inventory), [ADR-0092](0092-image-rootfs-lifecycle.md) /
  [ADR-0093](0093-private-image-uploads.md) (the DB `image_catalog` — this ADR revises the
  catalog *source*: YAML-seed → config-reconcile), and the reconciler drift-repair loop
  ([ADR-0021](0021-reconciler-loop-drift-repair.md)).
- **Spec:** [`../superpowers/specs/2026-06-14-systems-config-consolidation-design.md`](../archive/superpowers/specs/2026-06-14-systems-config-consolidation-design.md)
- **Issue:** [#385](https://github.com/randomparity/kdive/issues/385) (fault-inject capacity is
  hardcoded/incomplete — fixed by Phase 2's config-declared capacity).

## Context

Operator-configurable *inventory* is scattered and partly embedded in code: rootfs image
definitions in `images/seed_data/rootfs/*.yaml` and inline YAML in `admin/default_fixtures.py`;
the remote base image name as a hardcoded literal (`REMOTE_BASE_IMAGE_NAME`); the fault-inject
resource's capacity hardcoded and missing `vcpus`/`memory_mb` (#385); and provider connection
config as singleton env vars (`KDIVE_REMOTE_LIBVIRT_URI`, …) that admit only one host per kind.

The MCP coverage-campaign reruns repeatedly hit this: images had to exist in code to be seeded,
fault-inject could not be allocated at all (#385), and a second remote-libvirt host could not be
expressed. We want one declarative location for "what exists," loaded into the DB, supporting
multiple instances per provider — without dragging operational secrets into that file or
discarding the DB's role as the runtime index of S3-carried images.

## Decision

Introduce a single declarative inventory file, `systems.toml`, reconciled into the database by a
merge engine, governed by **three ownership layers**:

- **Config** (`systems.toml`) owns identity, source-intent, policy, economics, and connection:
  `provider/name/arch`, image source, `cost_class`, `concurrent_allocation_cap`, instance `uri`,
  secret *refs* (never secret material), declared sizing/capabilities. An image source is one of
  `s3` (bytes already in the object store), `build` (a recipe the build plane realizes to S3), or
  `staged` (a reference to an **operator-staged provider volume** by name — the current reality for
  the remote base image, so `REMOTE_BASE_IMAGE_NAME` becomes a `staged` `[[image]]` entry rather
  than a code literal; this is what Phase 1 removes from code).
- **Discovery** (reconciler host probes) owns physical hardware it alone enumerates: real host
  vcpus/memory, PCIe BDFs.
- **Runtime** (DB only) owns realized facts: S3 `object_key`, `digest`, publish `state`,
  project-private uploads, allocations/systems.

A `managed_by` column (`config` | `discovery` | `runtime`) on `image_catalog`, `resources`, and
`build_hosts` makes ownership enforceable. The reconcile engine upserts config-owned fields keyed
by a row's **stable identity** — `(provider, name, arch)` for images, the instance **`name`** for
resources/build_hosts (not `uri`/`host_uri`, which is not unique across multiple synthetic
instances) — **never** writes runtime-owned fields, **prunes** only rows it owns whose identity
left config, and is idempotent. Because a resource row mixes config- and discovery-owned fields,
`managed_by` governs **existence** (who creates/prunes) while a **config overlay** applies declared
attributes onto the resource's `capabilities` jsonb keyed by that same instance `name` — the
overlay supplies the fault-inject `vcpus`/`memory_mb` that #385 lacks. The resource identity moves
from today's `host_uri` (registration is currently "idempotent by `host_uri`") to a new `name`
column with the uniqueness constraint `(kind, name)` — so multiple `[[fault_inject]]` instances
that share `host_uri = fault-inject://local` (the Phase-3 multi-instance goal) coexist. A
**discovered** row binds to its config instance by `host_uri` and inherits that instance's `name`
(so the overlay lands on the right row); a discovered host with no config instance gets a
deterministic generated `name`.

**Prune is non-destructive (same contract as the reaper, below).** Pruning a config row obeys
refuse-if-live: an image row that is `registered` with **dependent systems** (resolved via the
`system → image` reference; establishing that reference if absent is a Phase-1 prerequisite — else
the guard degrades safely to "refuse prune of any `registered` image, let GC reclaim"), or a
resource with **live allocations**, is **not** deleted on disappearing from the file. The reconcile **cordons
only** — it stops *new* placement on the resource and surfaces the still-busy row in the reconcile
diff — but it **never auto-drains** (eviction of running systems is the explicit destructive
operator action, `resources.drain`/`deregister --force`, never a side effect of a file edit). The
row is finally removed once it is naturally idle. "Live" reuses admission's non-terminal-System
predicate (ADR-0109): a `crashed` System under live crash-debug counts as live and is preserved.
Pruning an image never deletes its S3 object inline; object reclamation stays the existing
image-GC path (a pruned catalog row leaves an orphan the GC collects once unreferenced), so a
file edit can never irreversibly destroy image bytes or running systems.

**Migration strategy.** All schema this ADR needs — `managed_by` on `image_catalog`/`resources`/
`build_hosts`, plus the resource `affinity` and `lease_expires_at` columns — is **authored up
front in the Phase-1 migration** (additive, forward-only, ADR-0015; single-migration-owner, as
migration 0023 did for M2.4). Later phases populate and read columns that already exist; the
engine treats an absent value as the pre-change default (see affinity below), so no phase depends
on a migration a later phase adds.

The engine runs both as a `kdive reconcile-systems` CLI (deploy `migrate → seed` + on demand) and
as a reconciler-loop pass (drift); an `ops.reconcile_now`-style MCP trigger (platform_admin, since
it can prune) lets an agent force it. The file path is `KDIVE_SYSTEMS_TOML` (default
`./systems.toml`); in k8s it is a mounted ConfigMap. Operational/secret config stays in the
`KDIVE_*` registry. The inventory pass is **fault-isolated**: a parse/validate failure (the common
case — a bad operator edit) reports the error and **skips the pass**, leaving the last-good DB
state intact and **never aborting the sibling reconciler repairs** (leaked-domain / leaked-`active`
reapers keep running).

`managed_by` is also the **declarative/imperative boundary**: declarative bring-up writes
`managed_by='config'` rows the reconciler owns; agent-native MCP tools write `managed_by='runtime'`
rows the reconciler never prunes/overwrites. The two own disjoint row-sets; on identity collision
(same instance `name`) declarative wins — reconcile **adopts** the runtime row: flips
`managed_by → config`, **clears `lease_expires_at`** (config rows are never lease-reaped), and
**takes the config-declared affinity** (default global), so an adopted row behaves exactly like
one created from the file. Reconcile and the registration tools **serialize on the identity** (a
row/advisory lock on `name`) so a prune cannot race a re-`register`; and `resources.register` of a
`name` that already exists as a `config` row is **rejected**, not silently shadowed. Runtime
resource registration (`resources.register`/`deregister`,
platform_admin; deregister-with-live destructive) defaults a resource to the registering
**project's affinity** (a new owner/allowlist column; admission checks it). The affinity column
defaults **global** (`NULL` = global): every pre-existing discovered resource and every
config-declared instance is global unless explicitly scoped, so the new admission check is a strict
no-op for current behavior — no allocation that works today regresses on rollout. Only an
agent-registered runtime resource defaults to its registering project. Each carries a
**`lease_expires_at`** the agent renews; the reconciler reaps a
runtime resource on lease expiry or sustained unreachability but **cordons (stops new placement) /
refuses — never auto-draining live work**, identical to the config-prune contract above (mirrors
the project-private image `expires_at` TTL, the leaked-`active` allocation reaper, ADR-0109, and
the probe-guest heartbeat reaping).

Delivered in four phases: (1) schema + engine + images (removes image definitions from code);
(2) resources — capacity/cost merged with discovery (fixes #385); (3) multi-instance + build
hosts (removes the singleton `KDIVE_REMOTE_LIBVIRT_*` env vars); (4) runtime mutation —
`resources.register`/`deregister`/`renew`, per-project affinity, lease + reachability reaping.

## Consequences

- Image and host definitions leave the codebase; a guard test keeps them out.
- `allocations.request(kind=fault-inject)` works (capacity declared), unblocking the synthetic
  lifecycle over MCP.
- Multiple instances per provider are expressible and independently allocatable (selection by
  `resource_id`, or any-available by `kind` — no allocation-API change).
- An agent can add/remove a system live (platform_admin), scoped to its project by default, with
  leaked additions auto-reaped — no permanent shared capacity left by a vanished agent. Adds a
  resource owner/affinity column (admission now checks it) and a `lease_expires_at` column.
- Revises ADR-0092/0093 (catalog source) and sharpens the ADR-0087 boundary; the DB remains the
  runtime S3 index, now loaded from config rather than packaged YAML.
- A file edit cannot irreversibly destroy in-use state: config-prune of a busy row **cordons
  only** (never auto-drains running systems) and never deletes image S3 bytes inline (object
  reclamation stays the existing unreferenced-image GC); eviction stays an explicit destructive op.
- All schema (the three `managed_by` columns + resource `affinity` + `lease_expires_at`) is
  authored in the Phase-1 migration (additive, forward-only); later phases only populate/read it.
- New operational dependency: the reconciler must be able to read `systems.toml` (a ConfigMap in
  k8s). A parse/validate failure fails fast **without half-applying** and is **isolated** from the
  sibling reconciler repairs, which keep running against the last-good state.
