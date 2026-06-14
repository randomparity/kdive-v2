# ADR 0112 — Declarative systems inventory config (`systems.toml`)

- **Status:** Proposed
- **Date:** 2026-06-14
- **Depends on:** [ADR-0087](0087-config-registry.md) (the `KDIVE_*` operational-config
  registry — this ADR draws the boundary: env owns operational/secret runtime config,
  `systems.toml` owns declared inventory), [ADR-0092](0092-image-rootfs-lifecycle.md) /
  [ADR-0093](0093-private-image-uploads.md) (the DB `image_catalog` — this ADR revises the
  catalog *source*: YAML-seed → config-reconcile), and the reconciler drift-repair loop
  ([ADR-0021](0021-reconciler-loop-drift-repair.md)).
- **Spec:** [`../superpowers/specs/2026-06-14-systems-config-consolidation-design.md`](../superpowers/specs/2026-06-14-systems-config-consolidation-design.md)
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
  secret *refs* (never secret material), declared sizing/capabilities.
- **Discovery** (reconciler host probes) owns physical hardware it alone enumerates: real host
  vcpus/memory, PCIe BDFs.
- **Runtime** (DB only) owns realized facts: S3 `object_key`, `digest`, publish `state`,
  project-private uploads, allocations/systems.

A `managed_by` column (`config` | `discovery` | `runtime`) on `image_catalog`, `resources`, and
`build_hosts` makes ownership enforceable. The reconcile engine upserts config-owned fields keyed
by identity, **never** writes runtime-owned fields, **prunes** only rows it owns whose identity
left config, and is idempotent. Because a resource row mixes config- and discovery-owned fields,
`managed_by` governs **existence** (who creates/prunes) while a **config overlay** applies
declared attributes onto the resource's `capabilities` jsonb keyed by `uri`/`host_uri` — the
overlay supplies the fault-inject `vcpus`/`memory_mb` that #385 lacks.

The engine runs both as a `kdive reconcile-systems` CLI (deploy `migrate → seed` + on demand) and
as a reconciler-loop pass (drift). The file path is `KDIVE_SYSTEMS_TOML` (default `./systems.toml`);
in k8s it is a mounted ConfigMap. Operational/secret config stays in the `KDIVE_*` registry.

Delivered in three phases: (1) schema + engine + images (removes image definitions from code);
(2) resources — capacity/cost merged with discovery (fixes #385); (3) multi-instance + build
hosts (removes the singleton `KDIVE_REMOTE_LIBVIRT_*` env vars).

## Consequences

- Image and host definitions leave the codebase; a guard test keeps them out.
- `allocations.request(kind=fault-inject)` works (capacity declared), unblocking the synthetic
  lifecycle over MCP.
- Multiple instances per provider are expressible and independently allocatable (selection by
  `resource_id`, or any-available by `kind` — no allocation-API change).
- Revises ADR-0092/0093 (catalog source) and sharpens the ADR-0087 boundary; the DB remains the
  runtime S3 index, now loaded from config rather than packaged YAML.
- New operational dependency: the reconciler must be able to read `systems.toml` (a ConfigMap in
  k8s); a parse error fails the reconcile fast rather than half-applying.
