# ADR 0089 — Operator CLI (`kdivectl`) as an authenticated MCP client (M2.2)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0010](0010-fastmcp-framework-auth.md) (the FastMCP
  streamable-HTTP framework + `JWTVerifier` bearer-token auth the CLI authenticates against),
  [ADR-0006](0006-oidc-rbac-attribution.md) (the OIDC/RBAC `(principal, agent_session)`
  attribution model the CLI's calls derive to), [ADR-0020](0020-rbac-audit-gate-implementation.md)
  (the RBAC roles, audit record, and destructive-op gate the mutating verbs route *around* via the
  M1.3 break-glass path), [ADR-0087](0087-config-registry.md) (the
  `KDIVE_*` registry the CLI resolves the server URL and token settings from),
  [ADR-0088](0088-deployment-packaging.md) (the image the `kdivectl` entry point ships in).
- **Spec:** [`../superpowers/specs/2026-06-10-m22-admin-cli-design.md`](../superpowers/specs/2026-06-10-m22-admin-cli-design.md)
- **Milestone:** M2.2

## Context

The M2.x band's M2.2 milestone delivers a supported administrative surface for operators
(`platform_admin` / `platform_operator`) that replaces the author's ad-hoc `psql`/`mc`
poking. The band design fixes the policy (two scopes — read-only first, then mutating; the
same OIDC + RBAC boundary as the MCP surface, not a bypass; mutations through the break-glass
path) and explicitly leaves the integration mechanism — "the HTTP API or an in-process path"
— to this milestone's spec.

Two facts from the codebase decide it:

- **There is no HTTP REST API.** The only transport is the FastMCP streamable-HTTP server
  (`python -m kdive server`) with `JWTVerifier` auth and a `DenialAuditMiddleware`. The
  operator-facing ops MCP tools already exist and are platform-role gated.
- **kdive issues no tokens — it is a pure verifier.** `build_verifier()` validates
  `iss`/`aud`/signature against the IdP's JWKS and reads `platform_roles` from claims. There
  is no user table or "first admin" inside kdive; identity lives in the external IdP
  (`navikt/mock-oauth2-server` in dev/CI, a real IdP in prod).

## Decision

1. **`kdivectl` is an MCP client to the existing FastMCP server**, not a new transport and
   not an in-process service-layer caller. It reaches the service layer only over the
   JWT/HTTP MCP surface the agents use, inheriting the verifier, the RBAC gate, and the
   audit middleware unchanged. This is the lowest-cost *correct* option: it reuses the one
   place auth + audit + RBAC are already solved, and adds no second front door to secure.

2. **Three layers.** (a) A generic client core (`kdive.cli`) — server-URL resolution, token
   attach, error→exit-code mapping, and a `tool call <name> --json` passthrough that makes
   the whole MCP surface reachable by humans. (b) Curated operator verbs over the core,
   operator-shaped (table default, `--json` everywhere); read-only first, mutating verbs
   through the break-glass path. (c) `kdivectl login`, the token-acquisition affordance.

3. **Bootstrap is an IdP concern, not a kdive side-door.** Because kdive only verifies
   tokens, the first admin is provisioned in the IdP. `kdivectl login` drives the mock-OIDC
   authorization-code flow with a parameterized principal (`--as admin|operator|viewer`) for
   dev/CI and the boundary test; prod supplies a token via `KDIVE_TOKEN` or a credentials
   file. A full real-IdP interactive flow (PKCE / device-code) is deferred — the
   RBAC-boundary exit test needs only the mock's parameterized-principal capability.

4. **Mutations route through the M1.3 break-glass path** (`mcp/tools/ops/breakglass.py`,
   `services/allocation/release.py`), not the per-allocation iteration gate
   (`security/authz/gate.py`), which is allocation/profile-scoped for an agent iterating,
   not an operator administering cross-project.

5. **Boundary invariant, enforced by test.** Every action is an authenticated MCP call
   attributed under `(principal, operator-cli)`; an under-privileged principal is denied and
   audited. `kdive.cli.commands.*` imports no `kdive.services.*` and reads no DB /
   object-store credential — asserted on the import graph, not a string grep.

6. **Two net-new read-only MCP tools** — `secrets.list` (presence only, never values) and
   `fixtures.list` (rootfs/fixture catalog) — because no read tool exists for either today.
   They are added to the MCP surface so agents gain them too (operators get no capability
   agents lack).

## Consequences

- The operator surface introduces no new transport, no new auth path, and no new audit sink:
  it is the same FastMCP server. RBAC is only as strong as the IdP's role mapping — dev (mock
  issues any claims) and prod (real IdP role mapping) diverge entirely at the IdP, not in
  kdive.
- `kdivectl` becomes the delivery vehicle for the M2.3 `doctor` verb and the M2.4
  image-lifecycle verbs; the core is designed to host them without rework.
- `kdivectl` ships as a `console_scripts` entry point in the existing `kdive` wheel/image —
  no second distributable.
- Deferring real-IdP interactive login means prod operators must obtain a token out-of-band
  until a later milestone; `KDIVE_TOKEN` / credentials-file ingestion is the bridge.
