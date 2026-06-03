# ADR 0004 — First slice targets local libvirt/QEMU

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #4 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

Proving the new architecture on proven infrastructure (local libvirt/QEMU) before
remote/cloud/bare-metal de-risks the design. Note the M1.5 fault-injection
provider exists to stress the seams that a local-only slice cannot. See the
spec's "Roadmap".

## Decision

M0 will implement exactly **one provider — local libvirt/QEMU — end-to-end across
all nine planes** before any remote, cloud, or bare-metal provider exists. The
walking skeleton runs on this provider on the new architecture; the plane
interfaces are defined here but stressed for portability only later (M1.5
fault-injection, M2 remote).

## Consequences

- Proves the end-to-end wiring and the domain model on infrastructure with few
  failure modes, cheaply and reproducibly.
- Does **not** prove seam portability or behavior under real leasing, secret
  resolution, or hardware failure — explicitly the job of M1.5 and M2 (see the
  spec's "Roadmap").
- Some plane contracts (cancel/cleanup guarantees, capability mismatch) are
  under-exercised with one provider; the M1.5 mock exists to exercise them before a
  real remote provider.
- Local-libvirt allocation is "always-yes" for chargeback but still
  capacity-admitted against a concrete concurrent-System / resource cap on the
  host, so M0 fails closed instead of thrashing the single host.

## Alternatives considered

- **Start with a remote or cloud provider.** Rejected: it combines network,
  leasing, secret, and hardware failure modes at once, before the model is proven.
- **Start with a mock provider only.** Rejected: a mock cannot prove the real
  kernel build/boot/debug path. The mock is added at M1.5 to stress the seams the
  real local path leaves slack, not to replace it.
