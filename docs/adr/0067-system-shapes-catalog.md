# ADR 0067 — System shapes catalog + selector unification (M1.4)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0007](0007-metering-budgets-admission.md) (the
  `Selector` and the size-weighted cost/admission gate a shape resolves into),
  [ADR-0024](0024-provisioning-profile-model-shape.md) (the provisioning profile whose
  sizing a shape now feeds), [ADR-0062](0062-platform-operations.md) (the
  `platform_operator` runtime-knob tool pattern `shapes.set` mirrors).
- **Spec:** [`../specs/m1.4-system-catalog-scheduling.md`](../specs/m1.4-system-catalog-scheduling.md)

## Context

The top-level design promises named system **shapes** (`small … max`) plus **full
custom** configuration over the provisioning profile. Today neither exists: the
allocation `Selector` is a bare `{vcpus, memory_gb, cost_class}` (`domain/cost.py`), and
the provisioning profile carries its own `vcpu` / `memory_mb` / `disk_gb` independently —
the allocation size a project is billed for and the size the System actually boots at can
silently disagree. An agent sizing a request has to hand-pick raw numbers with no curated
presets and no guarantee the booted machine matches what was admitted.

A shape is shared fleet configuration that operators want to tune without a deploy (add a
`gpu-xl` preset, retire `max`), the same operational shape as M1.3's cost-class
coefficients and per-host capacity caps.

## Decision

We will add a **`system_shapes` table** — `name` (PK), `vcpus`, `memory_mb`, `disk_gb`,
optional `pcie_match`, `updated_at` — seeded by migration with `small` / `medium` /
`large` / `max`, and `platform_operator` tools `shapes.set` (upsert) and `shapes.delete`
over it, with a `viewer` `shapes.list`. The allocation **selector accepts a shape name XOR
a full-custom sizing**.

**The resolver's output contract.** Resolving a shape yields one **sizing tuple**
`{vcpus, memory_mb, disk_gb, pcie_match?}`. Admission prices and capacity-checks only the
subset the `Selector` models — `vcpus` and `memory_gb` (`domain/cost.py`) — so the
resolver maps `memory_mb → memory_gb`; **shape `memory_mb` is constrained to whole-GB
multiples** (`shapes.set` rejects a non-multiple as `configuration_error`) so the mapping
is exact and a shape can never price a fractional GB the `Selector` cannot represent.
`disk_gb` and `pcie_match` are **not** `Selector` fields — they are carried to provisioning
(`disk_gb`) and to PCIe admission (`pcie_match`, the ADR-0068 matcher), not to the cost
gate. A shape fixes **size only**: `cost_class` (and therefore price) is resolved
admission-side from the chosen Resource (`services/allocation_admission.py`), not from the
shape, so the same shape on a costlier host costs more.

**Size is authoritative at the allocation; provisioning derives from it, never
contradicts it.** A shape-sized allocation's resolved tuple is the authority. For such an
allocation the profile's `vcpu` / `memory_mb` / `disk_gb` — **required** `gt=0` fields in
the current schema (`profiles/provisioning.py`) — **become optional**: `systems.provision`
**constructs** them (frozen, ADR-0024/0003) **from** the resolved tuple before validation,
so the profile is built from the tuple rather than mutated, preserving the
immutable-request-inputs invariant. Supplying them is allowed only when they **equal** the
tuple; a conflicting restatement is `configuration_error`. This is a small ADR-0024 schema
delta — sizing fields conditional on a shape-sized allocation — recorded here as a
follow-on obligation. Full-custom takes the reverse direction: the profile's sizing is the
authority and the selector restates it; a mismatch is likewise rejected. Either way
admitted size and booted size are one number by construction, not by hope.

**The persisted identity is the resolved sizing snapshot, not the name — and it reuses
state the schema already stores.** The resolved sizing is already persisted: an Allocation
carries `requested_vcpus` / `requested_memory_gb` (the at-grant snapshot, ADR-0007 §3) and
a System carries its full `provisioning_profile` JSON, which already holds `vcpu` /
`memory_mb` / `disk_gb`. So the migration **does not mint parallel sizing columns**; it
adds only the nullable **`shape` name label** to each table (and, on `allocations`,
`requested_disk_gb` plus the resolved `pcie` claim where the existing `requested_*` snapshot
falls short). Availability and reuse matching read sizing from that **existing persisted
state**, so a later `shapes.set` that redefines `medium` does **not** retroactively re-size
existing allocations/systems and cannot make a stamped name silently denote a different
size — the unification holds across mutation, not only at resolve-time. The **match
predicate** (whether a `shape=medium` request reuses by sizing-tuple equality or by name) is
owned by ADR-0070; this ADR only guarantees the sizing is stably persisted and queryable.

## Consequences

- Admission and provisioning stop being able to disagree on size: the resolved tuple is
  the single source for the cost estimate, the **per-allocation** `≤ host` capacity check,
  and the libvirt domain. It does **not** add size-summed packing — aggregate
  oversubscription across concurrent allocations stays the count-based
  `concurrent_allocation_cap`'s job (`services/allocation_admission.py`), unchanged here;
  shapes size each claim, they do not bin-pack the host.
- Operators tune the catalog at runtime through the existing `require_platform_role` seam;
  no new authorization plumbing, and changing the catalog is no longer a deploy.
- New migration adds the `system_shapes` table and the nullable `shape` name label on
  `allocations` / `systems` (plus `requested_disk_gb` / resolved `pcie` where the existing
  `requested_*` snapshot falls short) — not parallel sizing columns; the seed is a data
  migration, bisectable like the cost-class seed. Seeded values **should** be sized ≤ the
  intended host's caps, but this is **not** checked at seed time (the seed runs before
  discovery registers any host): an over-large shape is inert — every request for it fails
  closed via `validate_against_resource` — until corrected by `shapes.set` or a larger host
  joins. The seed targets the M1 dev host, and `max` is sized to it, not to an aspirational
  fleet.
- Because the snapshot is the identity, `shapes.set` and `shapes.delete` are **catalog-only
  edits with no retroactive effect**: redefining or removing `medium` changes what a
  *future* `medium` resolution yields and makes future resolution of a deleted name fail
  (`configuration_error`), but existing allocations/systems keep their stamped snapshot and
  stay reuse-matchable. `name` is the catalog PK; the `shape` name on `allocations` /
  `systems` is a recorded label, **not** a foreign key, so `shapes.delete` is never
  FK-blocked and never orphans a live row.
- A shape's optional `pcie_match` couples this catalog to the PCIe model (ADR-0068). The
  grammar is owned by the ADR-0068 matcher (spec issue #4); until it lands, `shapes.set`
  **stores `pcie_match` opaquely** and admission is the validating authority (an
  unresolvable match fails closed at request time, not at catalog time). Once #4 lands,
  `shapes.set` additionally rejects malformed grammar early. `shapes.set` validates neither
  host availability nor that any host *has* the card — a shape may name a card no current
  host owns; admission fails closed.
- Full-custom remains first-class — the shape catalog is a convenience layer, not a
  gate; an agent that needs an off-catalog size still passes raw vcpus/memory/disk.

## Alternatives considered

- **Code-config seeded catalog (immutable at runtime).** Smaller surface, no operator
  tools, but every catalog change is a deploy — rejected because shape tuning is exactly
  the fleet-config knob operators were given runtime control over in M1.3, and the table
  costs one migration.
- **Shape replaces the profile sizing entirely (no full-custom).** Simplest selector, but
  the top-level design explicitly requires full-custom configuration; rejected.
- **Keep allocation size and profile size independent.** Zero migration, but preserves the
  silent-disagreement bug the unification fixes; rejected.
