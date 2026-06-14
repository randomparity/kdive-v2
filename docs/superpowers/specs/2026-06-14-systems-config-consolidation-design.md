# Systems Config Consolidation — Design

Consolidate every operator-configurable *inventory* element — images, provider instances,
build hosts, and their capacity/cost attributes — into a single declarative file,
`systems.toml`, and load it into the database through a merge-reconcile engine. Removes image
and host definitions currently embedded in the code, and adds first-class support for **multiple
instances per provider** (multiple remote-libvirt hosts, etc.).

Status: design. Companion ADR: `docs/adr/0112-systems-inventory-config.md`. Delivered in four
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

## Operating model: declarative file + runtime tools

Both bring-up and runtime changes are supported by partitioning ownership with `managed_by`, so
the two write paths own **disjoint** row-sets and never fight:

- **Declarative (bring-up / GitOps):** declare in `systems.toml`; reconcile makes the DB match.
  Rows are `managed_by='config'` — the reconciler updates them to the file and **prunes** them
  when their identity leaves the file. Reproducible, version-controlled, the way a fleet is stood
  up. In k8s the file is a mounted ConfigMap; an edit there (or `helm upgrade`) is the declarative
  runtime-change path.
- **Imperative (ad-hoc / agent-native):** MCP tools mutate the DB directly as
  `managed_by='runtime'` rows the reconciler **never** prunes or overwrites (the same invariant
  that protects project-private images). This is how an agent adds/removes a system live without
  editing a file. The imperative tools operate **only on `runtime`-owned rows**: `resources.
  deregister` of a `config`-owned instance is **rejected** (config instances are removed by editing
  the file — otherwise a deregister flaps against the next reconcile that re-asserts it).

**Adopt on collision:** if an identity (same instance `name`) appears in **both** the file and as
a runtime row, declarative wins — reconcile adopts it: flips `managed_by` to `config`, **clears
`lease_expires_at`** (config rows are never lease-reaped), and **takes the config-declared
affinity** (default global), so an adopted row behaves exactly like one created from the file.
Adoption therefore applies the config affinity — intentionally **widening** a previously
project-scoped runtime resource to global unless the file declares a scoped affinity; declare the
scope in the file to keep it private. Reconcile and the registration tools **serialize on the
identity** (a row/advisory lock on `name`)
so a prune cannot race a re-`register`, and `resources.register` of a `name` that already exists as
a `config` row is **rejected**, not silently shadowed. The reverse (removing it from the file) then
prunes it (subject to the non-destructive prune contract), surfaced in the reconcile diff.

**Reload trigger (three, same engine):** the reconciler loop auto-applies file changes within a
poll interval (content-hash-gated); `kdive reconcile-systems` forces an immediate apply; and an
`ops.reconcile_now`-style MCP trigger (platform_admin, since it can prune) lets an agent force it.
`KDIVE_SYSTEMS_TOML` sets the path.

## The reconcile engine

New package `src/kdive/inventory/`:

1. **Parser/validator** — `systems.toml` → a typed `InventoryDoc` (pydantic). Validation at
   parse time: identities unique; `source` is a discriminated union; an instance's `base_image`
   names a declared `[[image]]`; required fields per provider kind. Parse-time can only check that
   `base_image` *names* a declared image; whether that image is `registered` is a **runtime**
   fact, so reconcile additionally **surfaces (warns / marks the instance degraded) when an
   instance's `base_image` is not yet `registered`** — the unbuilt-base failure is reported at
   reconcile, not deferred silently to provision time.
2. **Per-entity reconcilers** sharing one merge contract:
   - upsert config-owned fields keyed by **stable identity** — `(provider, name, arch)` for images,
     `(kind, name)` for resources/build_hosts (not `uri`/`host_uri`, which is not unique across
     multiple synthetic instances);
   - **never** write runtime-owned fields (`object_key`, `digest`, `state`, project-private rows);
   - **prune** only rows it owns (`managed_by = 'config'`) whose identity vanished from config —
     and prune is **non-destructive**: a `registered` image with dependent systems, or a resource
     with live allocations, is **cordoned only** (no new placement) and surfaced in the reconcile
     diff, **never auto-drained** (eviction is the explicit destructive op); the row is removed
     once naturally idle. "Live" reuses admission's non-terminal-System predicate (ADR-0109, a
     `crashed` System under live crash-debug is preserved). Pruning an image never deletes its S3
     object inline — reclamation stays the existing unreferenced-image GC;
   - idempotent (re-run = no-op).

### Per-field ownership: existence vs. attribute-overlay

A resource row mixes config-owned (`cost_class`, caps) and discovery-owned (PCIe, real vcpus)
fields, so `managed_by` cannot be per-field. Resolved with a split:

- **`managed_by` governs existence** — who *creates and prunes* the row. `config` for declared
  instances/images; `discovery` for purely host-probed resources.
- A **config overlay** applies declared *attributes* onto the resource's `capabilities` jsonb
  regardless of who created the row, keyed by the instance **`name`**. This overlay is where the
  fault-inject `vcpus`/`memory_mb` (the #385 fix) and `cost_class`/caps are supplied.

The resource identity moves from today's `host_uri` (registration is currently "idempotent by
`host_uri`") to a `name` column with the uniqueness constraint `(kind, name)`, so multiple
`[[fault_inject]]` instances sharing `host_uri = fault-inject://local` (the Phase-3 goal) coexist.
A **discovered** row binds to its config instance by `host_uri` and inherits that instance's `name`
(so the overlay lands on the right row); a discovered host with no config instance gets a
deterministic `name` derived from its `host_uri` (so two such hosts never collide). `resource_id`
(the existing UUID) stays the **stable PK and FK target** for allocations/systems; `name` is a
mutable unique attribute, so a discovery→config rebind that renames a row never breaks references.

So `local-libvirt` exists from discovery (real hardware) and receives a config overlay
(cost/cap); `fault-inject` and remote instances exist from config; runtime fields are never
written by reconcile.

### Image realization

Stays the existing `defined → pending → registered` lifecycle (`image_catalog`). An image source
is one of three kinds:

- an `s3` entry references bytes already in the object store. Reconcile **confirms the object
  exists** with a HEAD (existence only — an S3 HEAD returns size/ETag, not a content sha256, and a
  multipart ETag is not a whole-object hash, so it can never stand in for the image-identity
  digest of ADR-0092). The content `digest` must therefore be **supplied in config** (or computed
  by streaming the object); the row reaches `registered` only with a verified digest, else stays
  `defined` + a warning (the reconcile as a whole still succeeds);
- a `build` entry reconciles to a `defined` row that the existing `images.build`/`publish` flow
  realizes to S3; reconcile never downgrades a realized row. (Reconcile does **not** itself trigger
  the build — a `defined` build entry is bootable only after an operator/agent runs `images.build`;
  the spec does not auto-build, so a fresh deploy that needs a built image must build it.);
- a `staged` entry references an **operator-staged provider volume** by name (the current reality
  for the remote base image — a host-local libvirt volume, neither an S3 object nor kdive-built).
  This is what replaces the `REMOTE_BASE_IMAGE_NAME` code literal. **Schema impact:** a staged image
  has no S3 `object_key`, but `image_catalog`'s `image_object_present` CHECK (migration 0023)
  requires `state != 'defined' ⟹ object_key IS NOT NULL`. Phase-1 migration **relaxes that CHECK**
  so a `registered` staged row may have `object_key IS NULL` and instead carry a non-null `volume`
  reference (a new `volume` column; exactly one of `object_key`/`volume` is set per non-`defined`
  row). A staged image reaches `registered` once its `volume` is named (no S3 round-trip).

### Where it runs

One engine, three triggers (above): the reconciler-loop spec (`reconcile_inventory`, drift
correction), the `kdive reconcile-systems` CLI (deploy-time `migrate → seed` + on demand), and the
`ops.reconcile_now`-style MCP trigger.

In k8s the reconciler reads `systems.toml` from a mounted **ConfigMap**; the path is configurable
(`KDIVE_SYSTEMS_TOML`, default `./systems.toml`). Operational/secret config stays `KDIVE_*` env.

## Runtime mutation: registration, ownership, lifecycle

The imperative path an agent uses to add/remove inventory live.

### Registration tools + authorization

Adding a provider instance is **adding shared platform capacity**, so the new
`resources.register` / `resources.deregister` tools are **`platform_admin`, mutating** — mirroring
the existing `build_hosts.register` (platform_admin). `register` takes the same fields as a
`[[remote_libvirt]]`/`[[local_libvirt]]`/`[[fault_inject]]` block, **preflights** (reachability
probe, secret refs resolve, `base_image` is registered), then writes a `managed_by='runtime'`
resource row. `deregister` of a resource with **live allocations** is **destructive-tier**
(platform_admin + typed confirmation / `--force`), like `ops.force_teardown`. Images and build
hosts reuse the existing `images.*` / `build_hosts.*` mutation tools as their runtime surface.

### Per-project resource affinity (new capability)

Today `resources` is **platform-global** (`0001_init.sql` has no `project`/`owner` column), so any
project can allocate against any resource. This design adds an **owner/affinity** column: a
resource may be **global** (any project) or **scoped** to an owning project and an optional
allowlist of projects. Config-declared instances default global (a fleet is shared); an
**agent-registered (runtime) resource defaults to the registering agent's project**. Allocation
admission (`allocations._resolve_resource`) gains an affinity check: a project may only place on a
global resource or one it is on the allowlist of. The column defaults **global** (`NULL` = global),
so every pre-existing discovered resource and every config-declared instance is global unless
scoped — the new admission check is a strict no-op for current behavior, no allocation that works
today regresses on rollout. New column + admission-path change + tests.

### Lease + reachability reaping (leak resistance)

A `managed_by='runtime'` resource carries a **`lease_expires_at`**: it is renewed via the
`resources.renew` tool (platform_admin, keyed to the `resource_id` — **not** bound to the original
registering session, so a lease survives an agent handoff) while in use. The reconciler reaps a runtime resource
when the lease **expires** OR it is **unreachable past a TTL** (extending the #359 reachability
probe) — but it **never silently destroys live work**: a resource with live allocations is
**cordoned only** (no new placement) and the reap is **refused and surfaced** — never auto-drained;
eviction stays the explicit destructive op (`resources.drain`/`deregister --force`). This is the
identical contract to config-prune above, and mirrors the leaked-`active` allocation reaper that
preserves a `crashed` System under live crash-debug (ADR-0109) and the probe-guest heartbeat
reaping (`reconciler/provider_reaping.py`). Config-managed resources are
removed by editing the file (no lease); only runtime resources carry a lease. This mirrors the
project-private image `expires_at` TTL already in `image_catalog`.

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
[image.source]                       # exactly one of s3 | build | staged
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-43.qcow2"
digest = "sha256:…"                  # required to reach `registered` (HEAD only confirms existence)

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"; format = "qcow2"; root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"                      # operator-staged libvirt volume (replaces REMOTE_BASE_IMAGE_NAME)
volume = "fedora-kdive-remote-base-43.qcow2"

# (a build-recipe image, for reference — realized to S3 by `images.build`, not by reconcile)
[[image]]
provider = "local-libvirt"
name = "fedora-kdebug-43"
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
foundation is shared by Phases 2–4.

### Phase 1 — schema + engine + images

- `systems.toml` schema v2 parser/validator (`src/kdive/inventory/`).
- **One migration authoring all schema this design needs** (additive, forward-only, ADR-0015;
  single-migration-owner): `managed_by` on `image_catalog`/`resources`/`build_hosts`; the
  `image_catalog` `volume` column + relaxed `image_object_present` CHECK (exactly one of
  `object_key`/`volume` on a non-`defined` row, for `staged` images); the resource `name` column +
  `(kind, name)` uniqueness; and the resource `affinity` + `lease_expires_at` columns. Later phases
  only populate/read columns that already exist.
- **`managed_by` backfill for pre-existing rows is a split** (load-bearing): existing `resources`
  rows came from discovery → backfill `managed_by='discovery'` (so the first reconcile does not
  prune a discovered host not yet declared in `systems.toml`); existing seeded `image_catalog`
  baseline rows are config-equivalent → backfill `managed_by='config'` (so reconcile fully owns the
  catalog, and a previously-seeded image the operator did not migrate into `systems.toml` is
  pruned under the cordon guard rather than stranded as an unowned orphan). `affinity` backfills
  global for all (see above).
- Reconcile-engine core + `reconcile_images`.
- `kdive reconcile-systems` CLI + reconciler-loop `reconcile_inventory` spec.
- **Delete**: `src/kdive/images/seed_data/`, the inline rootfs YAML in
  `src/kdive/admin/default_fixtures.py`, the `REMOTE_BASE_IMAGE_NAME` literal.

Outcome: zero image definitions remain in code; images load from `systems.toml`.

### Phase 2 — resources: capacity, cost, merge-with-discovery

- `reconcile_resources` config overlay (populates the Phase-1 `managed_by`/`name` columns).
- Fault-inject capacity declared in config; remove its hardcoded capability dict
  (`src/kdive/providers/fault_inject/discovery.py`).
- Merge semantics with discovery: config overlay keyed by the instance `name`; discovered rows
  bind to a config instance by `host_uri` and inherit its `name`.

Outcome: #385 fixed; cost/capacity declared, not hardcoded.

### Phase 3 — multi-instance + build hosts

- Array-of-tables for `[[remote_libvirt]]`/`[[local_libvirt]]`/`[[fault_inject]]` (multiple
  instances per kind).
- `reconcile_build_hosts` + base-image volume + component roots from config.
- **Delete**: the singleton `KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR,BASE_IMAGE}` env vars
  and the superseded `scripts/coverage_campaign/d1.env.template`.

Outcome: multiple instances per provider; the last hardcoded host config is gone.

### Phase 4 — runtime inventory mutation (agent-native)

The imperative path, built on the Phases 1–3 `managed_by` foundation (its columns — `affinity`,
`lease_expires_at` — were authored in the Phase-1 migration; Phase 4 populates and reads them).

- `resources.register` / `resources.deregister` / `resources.renew` tools (platform_admin;
  deregister-with-live is destructive-tier) writing `managed_by='runtime'` rows.
- Per-project resource **affinity** + admission-path check; runtime-registered resources default
  to the registering project's scope; config-declared and existing rows default global (no-op).
- `lease_expires_at` populated on runtime resources + a reconciler **reap spec** (lease expiry OR
  unreachable past TTL), **cordon-only / refuse-if-live — never auto-drain** (no silent destruction).
- Adopt-on-collision in the reconcile engine (a config identity adopts a matching runtime row,
  clearing its lease and taking the config affinity; registration serializes on identity).

Outcome: an agent can add/remove a system live, scoped to its project, with leaked additions
auto-reaped — no permanent shared capacity left by a vanished agent.

## What gets deleted (replace, don't deprecate)

`src/kdive/images/seed_data/` · the embedded rootfs YAML in `default_fixtures.py` ·
`REMOTE_BASE_IMAGE_NAME` · the fault-inject hardcoded caps in `discovery.py` · the
`KDIVE_REMOTE_LIBVIRT_*` singletons · `scripts/coverage_campaign/d1.env.template`.

## Error handling

- Parse/validate failure is **fail-fast**, naming the offending entry + field; reconcile is
  **all-or-nothing per entity type** (one transaction each) — a bad entry never half-applies.
- The inventory pass is **fault-isolated** in the reconciler loop: a parse/validate failure (a bad
  operator edit — the common case) reports the error and **skips the pass**, leaving the last-good
  DB state intact and **never aborting the sibling reconciler repairs** (leaked-domain /
  leaked-`active` reapers keep running).
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
  4. the config overlay supplies fault-inject `vcpus`/`memory_mb` (the #385 regression test);
  5. prune of an in-use row (registered image with dependent systems / resource with live
     allocations) **cordons, does not delete** — no live work is destroyed by a file edit;
  6. adoption of a runtime row clears its lease and takes the config affinity;
  7. a `staged` image registers with `volume` set / `object_key` NULL, and the relaxed
     `image_object_present` CHECK **rejects** a non-`defined` row with *both* or *neither* of
     `object_key`/`volume`;
  8. an `s3` image with no `digest` stays `defined` (never reaches `registered`).
- **Integration** on disposable Postgres (ADR-0019, drive handlers directly); the live-stack
  exercise registers providers + images from `systems.toml`.
- **Guard test**: no image **or host** definition remains in code — asserts `images/seed_data`
  absent, no inline rootfs YAML in `default_fixtures.py`, no `REMOTE_BASE_IMAGE_NAME`, **and no
  `KDIVE_REMOTE_LIBVIRT_*` singleton env reads** (the Phase-3 host-config removal).

## Risks

- **k8s file delivery**: the reconciler needs `systems.toml` mounted (ConfigMap); path
  configurable via `KDIVE_SYSTEMS_TOML`.
- **Boundary with ADR-0087/0092/0093**: captured in ADR-0112 (three-layer ownership + reconcile
  contract).
- **Multi-instance selection**: already supported — multiple resources of a kind select by
  `resource_id`, or any-available by `kind`; no allocation-API change.
- **Rollback past Phase 1**: the relaxed CHECK is forward-only (ADR-0015) and stays after a code
  rollback (it only widens what's allowed, so old code's always-`object_key` writes still pass).
  `staged` `image_catalog` rows are a new data shape, but pre-Phase-1 code never read the remote
  base from the catalog (it used the `REMOTE_BASE_IMAGE_NAME` literal), so a leftover staged row is
  inert to rolled-back code — no down-migration needed.
