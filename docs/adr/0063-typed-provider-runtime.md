# ADR 0063 — Typed ProviderRuntime is the active M0/M1 provider seam

- **Status:** Accepted
- **Date:** 2026-06-07
- **Deciders:** kdive maintainers
- **Supersedes for runtime assembly:** [ADR-0009](0009-capability-provider-dispatch.md) and
  [ADR-0022](0022-capability-registry-dispatch-impl.md)

## Context

ADR-0009 and ADR-0022 describe capability-based provider dispatch as the core provider seam:
providers advertise `(plane, operation, resource_kind)` capabilities, and core code asks a
`CapabilityRegistry` for a `BoundOp`. The implementation contains that registry and tests it
against fake providers, but the production server and worker runtime do not use it.

The actual M0/M1 runtime is `providers.composition.ProviderRuntime`: startup constructs one
local-libvirt implementation for each typed port (`Provisioner`, `Builder`, `Installer`,
`Controller`, `Retriever`, debug/introspection ports) and passes those ports to MCP registrars
and worker handlers. That path is deliberate for the single-provider milestones: it keeps the
entrypoint explicit, type-checkable, and simple while the service stabilizes the DB/job/state
spine.

Leaving the old dispatch ADRs as the apparent current architecture is misleading. New work could
extend the dormant registry instead of the runtime that actually serves requests.

## Decision

For M0 and M1, the active provider seam is **typed `ProviderRuntime` ports**, not capability
dispatch.

- `src/kdive/providers/composition.py` is the only production assembly point for concrete
  provider implementations.
- MCP tools and worker handlers request typed ports from `ProviderRuntime`; they do not ask
  `CapabilityRegistry` for `BoundOp`s.
- `src/kdive/providers/capability.py` remains in the tree as a quarantined prototype and test
  fixture for future multi-provider dispatch. It is not production runtime infrastructure until
  a later ADR reactivates it and wires runtime assembly through it.
- Documentation and source docstrings must describe this split explicitly: typed runtime ports
  are current behavior; capability dispatch is a future option.

## Consequences

- The single-provider runtime has one true extension point to follow: add or adapt a typed port
  and wire it in `ProviderRuntime`.
- `OpContract` and `BoundOp` cannot be cited as driving live job routing, destructive gating, or
  reconciler behavior in M0/M1. Those behaviors are implemented directly in MCP/worker/security
  code.
- A future multi-provider milestone can still use the existing registry code, but it must first
  write a new ADR that defines how `ProviderRuntime`, handler registration, job routing, and
  destructive-op policy consume `CapabilityRegistry.dispatch`.
- Tests that exercise `CapabilityRegistry` remain useful as prototype coverage, but they do not
  prove production dispatch behavior.

## Alternatives Considered

- **Wire production through `CapabilityRegistry` now.** Rejected for M1: it would touch every
  plane registrar and worker handler while no second provider exists to validate the extra
  dispatch layer.
- **Delete the registry.** Rejected for now: it captures useful design work for the later
  multi-provider milestone and has isolated tests. Quarantining it avoids misleading runtime
  claims without throwing away that prototype.
