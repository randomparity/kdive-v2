# ADR 0063 — Typed ProviderRuntime is the active M0/M1 provider seam

- **Status:** Accepted
- **Date:** 2026-06-07
- **Deciders:** kdive maintainers
- **Supersedes for runtime assembly:** [ADR-0009](0009-capability-provider-dispatch.md) and
  [ADR-0022](0022-capability-registry-dispatch-impl.md)
- **Superseded in part by:** [ADR-0066](0066-remove-capability-registry-prototype-from-src.md)
  for the in-tree prototype retention decision.

## Context

ADR-0009 and ADR-0022 describe capability-based provider dispatch as the core provider seam:
providers advertise `(plane, operation, resource_kind)` capabilities, and core code asks a
`CapabilityRegistry` for a `BoundOp`. At the time of this ADR, the implementation contained
that registry and tests against fake providers, but the production server and worker runtime
did not use it.

ADR-0066 later removed that prototype from production source after this ADR established typed
ports as the live runtime seam.

The actual M0/M1 runtime contract is `providers.runtime.ProviderRuntime`; startup constructs
one local-libvirt implementation for each typed port (`Provisioner`, `Builder`, `Installer`,
`Controller`, `Retriever`, debug/introspection ports) in `providers.composition` and passes
those ports to MCP registrars and worker handlers. That path is deliberate for the
single-provider milestones: it keeps the entrypoint explicit, type-checkable, and simple
while the service stabilizes the DB/job/state spine.

Leaving the old dispatch ADRs as the apparent current architecture is misleading. New work could
extend the dormant registry instead of the runtime that actually serves requests.

## Decision

For M0 and M1, the active provider seam is **typed `ProviderRuntime` ports**, not capability
dispatch.

- `src/kdive/providers/composition.py` is the only production assembly point for concrete
  provider implementations.
- MCP tools and worker handlers request typed ports from `ProviderRuntime`; they do not ask
  `CapabilityRegistry` for `BoundOp`s.
- The original decision to keep `src/kdive/providers/capability.py` as a quarantined prototype
  was superseded by ADR-0066; the prototype is no longer production source.
- Documentation and source docstrings must describe this split explicitly: typed runtime ports
  are current behavior; capability dispatch is a future option.

## Consequences

- The single-provider runtime has one true extension point to follow: add or adapt a typed port
  and wire it in `ProviderRuntime`.
- `OpContract` and `BoundOp` cannot be cited as driving live job routing, destructive gating, or
  reconciler behavior in M0/M1. Those behaviors are implemented directly in MCP/worker/security
  code.
- A future multi-provider milestone must first write a new ADR that defines how
  `ProviderRuntime`, handler registration, job routing, and destructive-op policy consume
  capability dispatch.

## Alternatives Considered

- **Wire production through `CapabilityRegistry` now.** Rejected for M1: it would touch every
  plane registrar and worker handler while no second provider exists to validate the extra
  dispatch layer.
- **Delete the registry.** Rejected in this ADR because the prototype still captured useful
  design work. ADR-0066 later accepted removal after the typed runtime seam had settled.
