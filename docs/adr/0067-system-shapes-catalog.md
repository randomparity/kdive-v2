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
a full-custom sizing**; a named shape resolves to the same `Selector` admission already
prices, and the resolved sizing flows into the provisioning profile so the admitted size
and the booted System size are one number. Each Allocation and System records the `shape`
name it resolved from (NULL for full-custom) for later availability and reuse matching.

## Consequences

- Admission and provisioning stop being able to disagree on size: the resolved tuple is
  the single source for the cost estimate, the capacity check, and the libvirt domain.
- Operators tune the catalog at runtime through the existing `require_platform_role` seam;
  no new authorization plumbing, and changing the catalog is no longer a deploy.
- New migration adds the `system_shapes` table and the `allocations.shape` /
  `systems.shape` columns; the seed is a data migration, bisectable like the cost-class
  seed.
- A shape referencing a `pcie_match` couples this catalog to the PCIe model
  (ADR-0068) — `shapes.set` validates the match-spec grammar but not host availability
  (a shape may name a card no current host has; admission, not the catalog, fails closed).
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
