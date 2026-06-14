# Systems Config Consolidation — Design

Consolidate every operator-configurable *inventory* element — images, provider instances,
build hosts, and their capacity/cost attributes — into a single declarative file,
`systems.toml`, and load it into the database through a merge-reconcile engine. Removes image
and host definitions currently embedded in the code, and adds first-class support for **multiple
instances per provider** (multiple remote-libvirt hosts, etc.).

Status: design. Companion ADR: `docs/adr/0112-systems-inventory-config.md`. Delivered in three
phases, each its own implementation plan.

## Problem

Inventory facts are scattered and partly hardcoded:

- Rootfs image definitions live **in code**: `src/kdive/images/seed_data/rootfs/*.yaml` (packaged)
  and inline YAML strings in `src/kdive/admin/default_fixtures.py`.
- The remote provisioning base image name is a **hardcoded literal**:
  `REMOTE_BASE_IMAGE_NAME = "fedora-kdive-remote-base-43"`
  (`src/kdive/providers/remote_libvirt/rootfs_build.py:49`).
- The fault-inject resource's capacity is **hardcoded and incomplete** — its discovery omits
  `vcpus`/`memory_mb`, so `allocations.request(kind=fault-inject)` is denied `configuration_error`
  (issue #385; `src/kdive/providers/fault_inject/discovery.py:90`).
- Provider connection config is **singleton env vars** (`KDIVE_REMOTE_LIBVIRT_URI`,
  `…_CLIENT_CERT_REF`, `…_GDB_ADDR`, `…_BASE_IMAGE`), so only one remote-libvirt host can be
  configured.

The goal: one configuration location for "what exists," loaded into the DB, with multiple
instances supported. **Not** in scope: operational/secret runtime config (DB URL, S3 credentials,
OIDC issuer, secrets) stays in the `KDIVE_*` registry (ADR-0087). `systems.toml` holds *refs* into
the secret store, never secret material.

## Three ownership layers

The design rests on a strict separation of which layer owns which fields of a row.

| Layer | Owns | Examples |
|---|---|---|
| **Config** (`systems.toml`) | identity, source-intent, policy, economics, connection | `provider/name/arch`, image source, `cost_class`, `concurrent_allocation_cap`, instance `uri`, secret *refs*, declared sizing/capabilities |
| **Discovery** (reconciler probes hosts) | physical hardware it alone enumerates | real host vcpus/memory, PCIe BDFs |
| **Runtime** (DB only) | realized facts | S3 `object_key`, `digest`, publish `state`, project-private uploads, allocations/systems |

This revises ADR-0092/0093: the rootfs catalog source moves from "read-only YAML seeded into the
DB" to "config → merge-reconcile → DB." The DB remains the runtime index of S3-carried images
(`object_key`/`digest` per row); config owns only the declared identity and source-intent.

## The reconcile engine

New package `src/kdive/inventory/`:

1. **Parser/validator** — `systems.toml` → a typed `InventoryDoc` (pydantic). Validation at
   parse time: identities unique; `source` is a discriminated union; an instance's `base_image`
   names a declared `[[image]]`; required fields per provider kind.
2. **Per-entity reconcilers** sharing one merge contract:
   - upsert config-owned fields keyed by identity;
   - **never** write runtime-owned fields (`object_key`, `digest`, `state`, project-private rows);
   - **prune** only rows it owns (`managed_by = 'config'`) whose identity vanished from config;
   - idempotent (re-run = no-op).

### Per-field ownership: existence vs. attribute-overlay

A resource row mixes config-owned (`cost_class`, caps) and discovery-owned (PCIe, real vcpus)
fields, so `managed_by` cannot be per-field. Resolved with a split:

- **`managed_by` governs existence** — who *creates and prunes* the row. `config` for declared
  instances/images; `discovery` for purely host-probed resources.
- A **config overlay** applies declared *attributes* onto the resource's `capabilities` jsonb
  regardless of who created the row, keyed by `uri`/`host_uri`. This overlay is where the
  fault-inject `vcpus`/`memory_mb` (the #385 fix) and `cost_class`/caps are supplied.

So `local-libvirt` exists from discovery (real hardware) and receives a config overlay
(cost/cap); `fault-inject` and remote instances exist from config; runtime fields are never
written by reconcile.

### Image realization

Stays the existing `defined → pending → registered` lifecycle (`image_catalog`):

- an `s3` entry reconciles to `registered` after a HEAD verify of `object_key` (or stays
  `defined` + a warning if the object is missing — the reconcile as a whole still succeeds);
- a `build` entry reconciles to a `defined` row that the existing `images.build`/`publish` flow
  realizes to S3; reconcile never downgrades a realized row.

### Where it runs

Two triggers, one engine: a `kdive reconcile-systems` CLI (deploy-time, in the `migrate → seed`
step, and on demand) **and** a reconciler-loop spec (`reconcile_inventory`) for drift correction.

In k8s the reconciler reads `systems.toml` from a mounted **ConfigMap**; the path is configurable
(`KDIVE_SYSTEMS_TOML`, default `./systems.toml`). Operational/secret config stays `KDIVE_*` env.

## The `systems.toml` schema (v2)

Elevates the file from today's coverage-campaign helper into the app-consumed inventory
(`schema_version = 2`; the campaign `render-env`/`setup-commands` become one consumer).

```toml
schema_version = 2

# IMAGES → reconcile image_catalog (replaces images/seed_data/*.yaml + default_fixtures.py)
[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-43"
arch = "x86_64"; format = "qcow2"; root_device = "/dev/vda"
visibility = "public"
capabilities = ["kdive-ready-console", "ssh", "drgn"]
[image.source]                       # exactly one kind
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-43.qcow2"
digest = "sha256:…"                  # optional; reconcile backfills via HEAD

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"; format = "qcow2"; root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "build"
base = "fedora-43"
components = ["kdump", "guest-agent", "drgn"]

# PROVIDER INSTANCES → reconcile resources (multiple per kind)
[[remote_libvirt]]
name = "ub24-big"
uri = "qemu+tls://ub24-big.prod.pdx.drc.nz/system"
gdb_addr = "192.168.10.20"; gdbstub_range = "47000:47099"
client_cert_ref = "remote-clientcert.pem"
client_key_ref = "remote-clientkey.pem"   # pragma: allowlist secret - filename ref
ca_cert_ref = "remote-ca.pem"
base_image = "fedora-kdive-remote-base-43"   # → an [[image]] name
cost_class = "remote"; concurrent_allocation_cap = 1
shapes = ["small", "medium", "large", "max"]

[[fault_inject]]
name = "synthetic-1"
cost_class = "local"; concurrent_allocation_cap = 1
vcpus = 8; memory_mb = 8192          # #385 fix: declared, not hardcoded/missing
seed = 1337

[[local_libvirt]]
name = "workstation"
host_uri = "qemu:///system"
cost_class = "local"; concurrent_allocation_cap = 1
# vcpus/memory/PCIe come from discovery (merge)

# BUILD HOSTS → reconcile build_hosts
[[build_host]]
name = "ub24-big-ephemeral"
kind = "ephemeral-libvirt"
base_image_volume = "fedora-kdive-remote-base-43.qcow2"
workspace_root = "/var/lib/kdive/build"; max_concurrent = 2
```

## Phases

Each phase is independently shippable and bisectable; the Phase-1 engine + `managed_by`
foundation is shared by Phases 2–3.

### Phase 1 — schema + engine + images

- `systems.toml` schema v2 parser/validator (`src/kdive/inventory/`).
- `managed_by` column migration on `image_catalog`.
- Reconcile-engine core + `reconcile_images`.
- `kdive reconcile-systems` CLI + reconciler-loop `reconcile_inventory` spec.
- **Delete**: `src/kdive/images/seed_data/`, the inline rootfs YAML in
  `src/kdive/admin/default_fixtures.py`, the `REMOTE_BASE_IMAGE_NAME` literal.

Outcome: zero image definitions remain in code; images load from `systems.toml`.

### Phase 2 — resources: capacity, cost, merge-with-discovery

- `reconcile_resources` config overlay + `managed_by` on `resources`.
- Fault-inject capacity declared in config; remove its hardcoded capability dict
  (`src/kdive/providers/fault_inject/discovery.py`).
- Merge semantics with discovery (config overlay keyed by `uri`/`host_uri`).

Outcome: #385 fixed; cost/capacity declared, not hardcoded.

### Phase 3 — multi-instance + build hosts

- Array-of-tables for `[[remote_libvirt]]`/`[[local_libvirt]]`/`[[fault_inject]]` (multiple
  instances per kind).
- `reconcile_build_hosts` + base-image volume + component roots from config.
- **Delete**: the singleton `KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR,BASE_IMAGE}` env vars
  and the superseded `scripts/coverage_campaign/d1.env.template`.

Outcome: multiple instances per provider; the last hardcoded host config is gone.

## What gets deleted (replace, don't deprecate)

`src/kdive/images/seed_data/` · the embedded rootfs YAML in `default_fixtures.py` ·
`REMOTE_BASE_IMAGE_NAME` · the fault-inject hardcoded caps in `discovery.py` · the
`KDIVE_REMOTE_LIBVIRT_*` singletons · `scripts/coverage_campaign/d1.env.template`.

## Error handling

- Parse/validate failure is **fail-fast**, naming the offending entry + field; reconcile is
  **all-or-nothing per entity type** (one transaction each) — a bad entry never half-applies.
- A **missing S3 object** for an `s3` image degrades cleanly (row stays `defined` + a warning);
  the reconcile as a whole still succeeds.
- Object-store-unconfigured tolerance matches the existing `_seed_build_configs_step`: a no-S3
  bring-up degrades and realizes on a later reconcile.

## Testing

- **Parser/validation** units: well-formed + malformed TOML; the discriminated `source` union
  (property-based); cross-ref failure (`base_image` naming a missing image).
- **Reconcile merge** — the load-bearing invariants:
  1. a reconcile **never overwrites** a build-realized `object_key`/`digest`/`state`;
  2. prune removes **only** `managed_by='config'` rows absent from config;
  3. project-private/runtime rows are untouched;
  4. the config overlay supplies fault-inject `vcpus`/`memory_mb` (the #385 regression test).
- **Integration** on disposable Postgres (ADR-0019, drive handlers directly); the live-stack
  exercise registers providers + images from `systems.toml`.
- **Guard test**: no image/host definition remains in code (asserts `images/seed_data` absent,
  no inline rootfs YAML in `default_fixtures.py`, no `REMOTE_BASE_IMAGE_NAME`).

## Risks

- **k8s file delivery**: the reconciler needs `systems.toml` mounted (ConfigMap); path
  configurable via `KDIVE_SYSTEMS_TOML`.
- **Boundary with ADR-0087/0092/0093**: captured in ADR-0112 (three-layer ownership + reconcile
  contract).
- **Multi-instance selection**: already supported — multiple resources of a kind select by
  `resource_id`, or any-available by `kind`; no allocation-API change.
