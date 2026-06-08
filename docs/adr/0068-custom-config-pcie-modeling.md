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

**What model-only delivers, stated plainly.** M1.4 delivers **locating and reserving** the
right card — the device is modeled, discoverable across the fleet, and held for the
allocation's life — so an agent can secure the hardware it needs and the booking is honest.
It does **not** attach the card to the guest: the in-guest passthrough that actually makes
the device *debuggable* (vfio/`<hostdev>`) lands with the later wiring milestone. So this
milestone enables "find and hold the X710," not yet "debug the X710 driver from inside the
guest." The reservation is the durable half that the wiring milestone completes without a
model change.

The portable identity of a PCIe device is its `vendor:device` ID (e.g. `8086:1572` = Intel
X710) and its `class` code (e.g. `0200` = Ethernet controller); the host-local BDF slot
(`0000:3b:00.0`) is meaningless across hosts. A request must match on the former and
resolve to the latter on the chosen host.

The single dev host has no IOMMU passthrough plumbing wired, and standing that up is
hardware-fragile and orthogonal to the booking model. M0–M1 is "model the platform before
the provider does the physical work."

## Decision

We will model PCIe as a **selection axis, validated and claimed but not yet wired**.

**Static inventory vs. derived occupancy.** Host `capabilities.pcie_devices` becomes a
list of **static descriptors** `{bdf, vendor_id, device_id, class_code, label}`, populated
by discovery — **no `free` flag**. Occupancy is **not** stored in capabilities: a device is
free iff **no non-terminal allocation's `pcie_claim` holds it**. This is deliberate —
because nothing is physically wired (no vfio bind), discovery reads the host's unchanged
hardware and would always see a device as physically available, so a `free` flag in the
discovery-populated descriptor would reset on every re-scan and silently un-claim a
logically-booked device. Discovery owns the static descriptor; the claim table owns
occupancy.

**Matching and claiming.** A request or fleet query references a device by a **match
spec** — never by BDF:

- `vendor:device` — `4hex:4hex`, lowercase (e.g. `8086:1572`), an exact device-model match.
- `class=` — a `2hex` value matches the class high byte (e.g. `class=02` = any network
  controller), or a `4hex` value matches class+subclass exactly (e.g. `class=0200`).

One **matcher** resolves a match spec against the host's descriptors **minus the devices
held by an active claim**, and it backs request-side admission, the fleet availability
filter (ADR-0070), and the reuse filter. The device set a request resolves and claims is
the **union** of the request's explicit `pcie_devices: [...]` (spec issue #5) and, when the
allocation is shape-sized, the shape's `pcie_match` (ADR-0067) — a `gpu-xl` shape reserves
its GPU even when the request lists no extra device. The matcher resolves that union to
**distinct** free devices within the one request (two `8086:1572` specs claim two different
cards, never the same one); selection, the two failure modes (next paragraph), and the
claim all operate on the union.

**PCIe-aware host selection.** A request whose device union (above) is **non-empty** —
whether from explicit `pcie_devices` or a shape-only `pcie_match` — makes host selection
device-aware; it is **not** resolved PCIe-blind. Today `allocations.request` resolves a host
by id or by *kind* (`_resolve_resource` picks the first schedulable host of a kind,
PCIe-blind); a PCIe request instead selects a schedulable host that **has a free matching
device for every spec in the union**. So the fleet-vs-host determination is made at selection, not buried
in a single pre-chosen host: a card that **no schedulable host has** is the fleet-level
`configuration_error`; a card that exists but whose every match **is currently claimed** is
the capacity case below. (An agent may still pre-resolve a host via `resources.availability`
and request it by id; a by-id host that lacks a free match yields the same two-mode result.)

**Two distinct failure modes — config vs. capacity.** A spec that **no schedulable host's
descriptors match** is a `configuration_error` (the card does not exist in the fleet). A
spec whose only candidates **are all currently claimed** is a **capacity** denial — the
card exists but is busy — and therefore participates in the ADR-0069 pending queue: with
`on_capacity=queue` the request waits, and the promotion sweep re-resolves the device when a
claim frees. The matcher returns enough to tell the two apart; admission must not hard-deny
a merely-busy device.

**Claim identity (snapshot, not a stable handle).** `pcie_claim` persists a **snapshot** —
the matched `(vendor_id, device_id)` and the resolved `bdf`. There is **no** truly stable
software handle for a generic PCI device: libvirt's nodedev name (`pci_0000_3b_00_0`) is
itself the BDF respelled, so it carries the same host-local ephemerality and is recorded
only as a best-effort hint, never as identity. Staleness is bounded operationally: a host
reboot/PCI rescan that re-letters a BDF also tears down that host's Systems and its
short-lived lease allocations, so the window in which a snapshot BDF could mis-resolve is
small; a claimed device that has disappeared or moved at provision time resolves to
`stale_handle` and the reconciler frees the claim.

**Claim lifetime.** Admission resolves a requested match **inside the per-Resource lock**
(see Consequences) and persists the claim on the Allocation. The claim frees on **every
terminal allocation transition** — released, expired, failed, and the M1.3 break-glass
`force_release` / `force_teardown` — since occupancy is derived from non-terminal
allocations; the reconciler's leaked-infra sweep (ADR-0021) reaps claims held by
terminal/orphaned allocations.

No libvirt `<hostdev>` XML, IOMMU-group handling, or vfio binding is emitted — the booking
is a durable *reservation* (the device is not-claimable by another allocation for this
allocation's life), but no guest is attached and the device is **not** physically isolated
from the host. PCIe is **not** a cost input: kcu stays size×time on vcpus/memory.

## Consequences

- "Find me a host with a free X710" becomes one matcher call shared by three surfaces;
  the agent never enumerates hosts.
- The claim keeps availability counts honest under concurrency, **but only because device
  resolution-and-claim runs inside the per-Resource lock**, alongside the host-cap check in
  the grant section (ADR-0040) — not in admission's pre-lock validation phase (ADR-0007
  step 1), where two requests could both resolve the last free device before either claims.
  Only grammar/format validation belongs pre-lock; the resolve-and-claim is a locked
  read-modify-write. This is a placement constraint the implementation must honor, not a
  free property of the existing lock.
- A later provider-capability milestone adds the physical wiring **behind the same
  descriptor and claim** — the model does not change when passthrough lands, only the
  provisioner gains a step that reads the already-persisted `pcie_claim`.
- Discovery must learn to enumerate host PCIe devices; on local-libvirt this reads the
  host's libvirt nodedev inventory and `lspci` for the human `label`. Discovery writes only
  the **static** descriptor and never an occupancy flag, so a re-scan is idempotent against
  live claims.
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
