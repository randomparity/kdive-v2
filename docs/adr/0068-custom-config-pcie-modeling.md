# ADR 0068 — Custom config + PCIe capability modeling (M1.4)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0007](0007-metering-budgets-admission.md) (the
  fail-closed `≤ resource-caps` admission check a device match extends),
  [ADR-0023](0023-discovery-allocation-admission.md) (the discovery registration that
  populates host capabilities), [ADR-0067](0067-system-shapes-catalog.md) (a shape may name
  a `pcie_match`).
- **Spec:** [`../specs/m1.4-system-catalog-scheduling.md`](../specs/m1.4-system-catalog-scheduling.md)

## Context

Hardware-specific kernel bugs are tied to a specific card — a NIC model, a storage HBA, a
GPU. The top-level design lists Resource `capabilities` (PCIe) among the latent concepts
M1.4 realizes, and an agent chasing such a bug needs to **find a host with that card and
claim it**, not weed through hosts by hand. Today `capabilities` carries only scalar
ceilings (`vcpus`, `memory_mb`, `concurrent_allocation_cap`); there is no device
inventory and no way to request or locate one.

The portable identity of a PCIe device is its `vendor:device` ID (e.g. `8086:1572` = Intel
X710) and its `class` code (e.g. `0200` = Ethernet controller); the host-local BDF slot
(`0000:3b:00.0`) is meaningless across hosts. A request must match on the former and
resolve to the latter on the chosen host.

The single dev host has no IOMMU passthrough plumbing wired, and standing that up is
hardware-fragile and orthogonal to the booking model. M0–M1 is "model the platform before
the provider does the physical work."

## Decision

We will model PCIe as a **selection axis, validated and claimed but not yet wired**. Host
`capabilities.pcie_devices` becomes a list of **descriptors** `{bdf, vendor_id,
device_id, class_code, label, free}`, populated by discovery. A request or fleet query
references a device by a **match spec** — exact `vendor:device` or `class=NN`, never by
BDF. One **matcher** resolves a match spec against the free descriptors, and it backs
request-side admission, the fleet availability filter (ADR-0070), and the reuse filter.
Admission resolves a requested match to a concrete free BDF, **persists the claim on the
Allocation** (`pcie_claim` jsonb), and **frees it on release/expiry**. No libvirt
`<hostdev>` XML, IOMMU-group handling, or vfio binding is emitted — the booking is durable
and honest (the device is not-free for the allocation's life), but no guest is attached to
the device. PCIe is **not** a cost input: kcu stays size×time on vcpus/memory.

## Consequences

- "Find me a host with a free X710" becomes one matcher call shared by three surfaces;
  the agent never enumerates hosts.
- The claim keeps availability counts honest under concurrency — the per-Resource advisory
  lock that guards the host capacity check (ADR-0040) also guards the device claim, so two
  requests cannot both resolve the last free device.
- A later provider-capability milestone adds the physical wiring **behind the same
  descriptor and claim** — the model does not change when passthrough lands, only the
  provisioner gains a step that reads the already-persisted `pcie_claim`.
- Discovery must learn to enumerate host PCIe devices; on local-libvirt this reads the
  host's `lspci`/libvirt nodedev inventory.
- The honesty gap is explicit and documented: a claimed device is a reservation, not an
  attached device, until the wiring milestone. The spec records this as a non-goal so no
  one reads a claim as "passed through."

## Alternatives considered

- **Full passthrough wiring now** (emit `<hostdev>`, manage IOMMU groups, bind vfio).
  Real end-to-end passthrough, but hardware-fragile on the single dev host and a large
  surface orthogonal to the catalog/scheduler work; rejected for M1.4, deferred to a
  provider-capability milestone.
- **Match by BDF only.** Trivial to implement, but a BDF is host-local — the agent could
  not ask for "an X710 on whatever host has one," which is the whole feature; rejected.
- **Omit PCIe from M1.4.** Smallest scope, but drops the hardware-specific-bug workflow the
  feature exists to serve; rejected.
- **Price PCIe into kcu.** A device-hours surcharge, but the cost model is deliberately
  size×time and a surcharge needs a coefficient story with no current demand; rejected —
  PCIe stays a capacity constraint.
