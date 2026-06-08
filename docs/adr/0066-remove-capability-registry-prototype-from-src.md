# ADR 0066 — Remove capability-registry prototype from production source

- **Status:** Accepted
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Supersedes:** [ADR-0063](0063-typed-provider-runtime.md) where it retained
  `src/kdive/providers/capability.py` as an in-tree prototype.

## Context

ADR-0063 made typed `ProviderRuntime` ports the active M0/M1 provider seam, but it left
the earlier capability-registry prototype in `src/` with dedicated tests. That kept a second
provider-selection API visible beside the runtime path that actually serves MCP tools and
worker handlers.

The extra API is not wired into server startup, worker registration, job routing,
destructive-op gating, or resource admission. Keeping it in production source makes it easy
to extend the wrong seam.

## Decision

Remove the capability-registry prototype from production source and remove its dedicated
tests. The active provider seam remains `providers.composition.ProviderRuntime` and the
typed provider port protocols in `providers.ports`.

Historical design material stays in ADR-0009/ADR-0022 and the dated superpowers specs. A
future multi-provider milestone must write a new ADR before reintroducing capability-based
runtime dispatch.

## Consequences

- There is one production provider assembly path to follow: build or extend typed runtime
  ports and wire them through `ProviderRuntime`.
- `OpContract`, `CapabilityRegistry`, and `BoundOp` are no longer importable production
  APIs.
- Existing historical specs remain historical records, not live implementation contracts.
- A later multi-provider design can reuse the old ADR context, but it must introduce fresh
  production code and tests under a new accepted decision.
