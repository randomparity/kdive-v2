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
- **Spec:** [`../superpowers/specs/2026-06-10-m22-admin-cli-design.md`](../archive/superpowers/specs/2026-06-10-m22-admin-cli-design.md)
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
   JWT/HTTP MCP surface the agents use, inheriting the `JWTVerifier` and the RBAC gate
   unchanged. The one bounded server-side change it requires is in audit attribution (see
   decision 5): the middleware learns to record the caller's *client identity* so an
   operator-CLI call is distinguishable from an agent call. This is the lowest-cost
   *correct* option: it reuses the one place auth + RBAC are already solved, and adds no
   second front door to secure.

2. **Three layers.** (a) A generic client core (`kdive.cli`) — server-URL resolution, token
   attach, error→exit-code mapping, and a `tool call <name> --json` passthrough that makes
   the MCP surface reachable by humans. **The passthrough is read-only by policy**, enforced
   **fail-closed**: it allows only tools positively annotated `mutating: false` in their
   registration metadata and refuses everything else — anything mutating, unannotated, or of
   unknown classification. Because the default is refuse, the annotation is added
   **incrementally to the read-only tools the passthrough should expose**, not as a
   codebase-wide retrofit; an unannotated tool (existing or added later) is simply not
   reachable via `tool call` until someone deliberately marks it read-only, so nothing can
   silently slip through. (`JobKind`-ownership alone is an insufficient signal — it catches
   only async mutations, missing synchronous ones like a cordon or an idempotency-key reset —
   which is why the gate is a positive read-only annotation, not a mutation heuristic.) A
   blanket annotate-every-tool pass is *not* required by this design; if wanted for hygiene it
   is a separate, owned task. Every state change therefore goes through a curated verb; there
   is no generic escape hatch around the break-glass routing. (Server-side per-tool authz
   still applies regardless; the client-side refusal is a policy/UX guard, not the security
   boundary — a determined operator with a raw MCP client is still bounded only by server-side
   authz, which is the point of keeping every tool independently gated.) (b) Curated operator
   verbs over the core,
   operator-shaped (table default, `--json` everywhere); read-only first, mutating verbs
   through the break-glass path. (c) `kdivectl login`, the token-acquisition affordance.

3. **Bootstrap is an IdP concern, not a kdive side-door.** Because kdive only verifies
   tokens, the first admin is provisioned in the IdP. `kdivectl login` drives the mock-OIDC
   authorization-code flow with a principal parameterized on the **platform-role axis the CLI
   acts on** — `--platform-role platform_admin|platform_operator` (or none) — *not* the
   project-scoped `admin|operator|viewer` `Role` triad, which is a different axis
   (`security/authz/rbac.py`: project `Role` vs `platform_roles`). The operator surface and
   every mutating verb gate on `platform_roles`, so the boundary test drives a mutating verb
   twice: once with `platform_admin` (succeeds, audited) and once with **no platform role**
   (denied, audited) — proving the *platform* boundary, not a project-role boundary that
   could pass for the wrong reason. Prod supplies a token via `KDIVE_TOKEN` or a credentials
   file. A full real-IdP interactive flow (PKCE / device-code) is deferred — the boundary
   exit test needs only the mock's parameterized-claims capability.

4. **Every mutation `kdivectl` performs routes through the M1.3 break-glass path**
   (`mcp/tools/ops/breakglass.py`, `services/allocation/release.py`), not the per-allocation
   iteration gate (`security/authz/gate.py`), which is allocation/profile-scoped for an agent
   iterating, not an operator administering cross-project. This is a true invariant, not a
   description of the curated verbs only, *because* the generic passthrough is read-only by
   policy (decision 2a): there is no `tool call` route to a mutating tool, so the curated
   break-glass verbs are the sole mutation path the CLI exposes.

5. **Boundary invariant, enforced by test.** Every action is an authenticated MCP call,
   attributed in the audit log alongside the existing `(principal, agent_session)` tuple
   (ADR-0006) with the **caller's client identity**. The mechanism: `kdivectl` authenticates
   under a distinct OIDC `client_id` (surfaced in the token's `azp`/`client_id` claim), and
   the audit middleware records that client id on every entry — so an operator-CLI action is
   attributable as such without a second audit sink and without trusting a self-asserted
   header. **Prod prerequisite (named, not assumed):** operators MUST register a dedicated
   `kdivectl` OIDC client distinct from the agents' client; the dev mock issues it on demand.
   The failure mode is reusing the agents' client_id, which would silently collapse
   operator-CLI attribution into agent attribution — so it is not left to convention: the
   audit middleware records `actor` from a closed mapping (`kdivectl`-client → `operator-cli`,
   recognized agent client + `agent_session` → `agent`) and records **`actor=unknown`** for
   any token matching neither, rather than defaulting to `agent`. Absence of `agent_session`
   corroborates but is not relied on as a positive identifier. The M2.3 `doctor` SHOULD flag
   operator-CLI calls arriving under the agent client_id. An under-privileged principal
   driving a mutating verb is
   **denied and audited** (the exit test drives the same verb with and without the platform
   role — see decision 3). The whole `kdive.cli.*` package imports no `kdive.services.*` and
   reads no DB / object-store credential — asserted on the import graph (not a string grep),
   scoped to the entire CLI package so the boundary cannot erode through the transport or
   login modules, where a credential shortcut would be most tempting.

6. **Two net-new read-only MCP tools** — `secrets.list` (presence only, never values) and
   `fixtures.list` (rootfs/fixture catalog) — because no read tool exists for either today.
   Their authz scope differs and is set deliberately:
   - **`secrets.list` is platform-role-gated**, not on the general agent surface. Secret
     *presence* — which refs exist, across projects and the platform — is a reconnaissance
     primitive and an information-disclosure surface; exposing it to every tenant agent is
     the risk, not the goal. It is reachable only behind the `platform_admin`/`platform_operator`
     **role gate** — a plain platform-role check, *not* the heavier break-glass ceremony the
     mutating verbs route through (a read needs authorization, not break-glass) — and even then
     returns presence/metadata only, never values (a redaction test enforces this).
   - **`fixtures.list` is project-scoped** like the other catalog reads: an agent sees the
     fixtures available to its granted projects, an operator sees the platform view. The
     rootfs/fixture catalog is not sensitive in the way secret presence is.

   So "operators get no capability agents lack" holds for the project-scoped catalog reads,
   but secret *presence* is explicitly an operator-only capability — the one place the
   surfaces intentionally diverge, for disclosure reasons.

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
- **Cached-token handling is in scope** because the mutating verbs (`teardown`,
  `force-release`) carry `platform_admin` blast radius. `login` writes the token to a
  per-user credentials file created `0600` (parent dir `0700`); a world-readable token is
  treated as a defect, not a convenience. The CLI never logs the token and redacts it from
  error output.
- **Token expiry is fail-closed for destructive verbs.** Each mutating verb does a
  pre-flight token-validity/`exp` check and refuses to start a destructive action on a token
  that would expire within a small margin, rather than risk a mid-operation 401 leaving a
  half-torn-down state. With real-IdP refresh deferred, destructive verbs are specified to be
  **single-call and re-runnable** (idempotent against already-released/already-torn-down
  state), so re-authenticating and re-running converges rather than corrupting — the
  reconciler's drift-repair already tolerates the partial states these verbs target.
