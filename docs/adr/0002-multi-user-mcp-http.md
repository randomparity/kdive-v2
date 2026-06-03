# ADR 0002 — Multi-user service over MCP streamable HTTP

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #2 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

The service is remote and multi-user; agents (Claude Code, Codex) must
authenticate with scoped, on-behalf-of tokens rather than the PoC's implicit
local user over stdio. See the spec's "System topology" and "MCP tool surface".

## Decision

We make two separable decisions. **Transport:** the agent surface is exposed over
the MCP **streamable-HTTP** transport; the human surface (CLI / future UI) uses
the REST/gRPC channel from the spec's "System topology". **Auth model (both
surfaces):** every caller presents an **OIDC-issued OAuth2 bearer token (JWT)** in
the `Authorization` header, validated identically regardless of transport. The
service acts as an OAuth2 resource server: it validates the token's **signature,
issuer (`iss`), audience (`aud` = this service), and expiry** against the IdP's
JWKS, and derives the `principal` from the token subject. No implicit local
identity exists, and no caller-supplied identity (header or body) is trusted —
identity comes only from the signed token.

**Attribution (`agent_session`).** The `agent_session` is a claim **inside the
signed JWT**, issued by the IdP when an agent session is established (via RFC 8693
token exchange or a per-session token grant). The resource server trusts it only
because it is signed by the IdP; a value an agent places in the request is
ignored. Every call is attributed to `(principal, agent_session)`. Degradation to
`principal`-only attribution is **milestone-gated, not an open-ended fallback**:
M0 (single-operator, local) may run `principal`-only; from M1 (the OIDC/RBAC
hardening milestone) a signed `agent_session` is **required** — the service
refuses destructive ops (and, for the strict profile, refuses to start) without
it. Attribution is never silently inferred from request data, and a `principal`-
only deployment is a recorded, explicit configuration, not a default.

**Tenant/project scope.** A principal may belong to several projects. The
tenant/project a request acts under comes from an explicit request parameter
**validated against the principal's IdP-asserted memberships**; a request naming a
project the principal does not belong to is denied. The detailed
membership/role model is owned by [0006](0006-oidc-rbac-attribution.md) and the
budget scope by [0007](0007-metering-budgets-admission.md); this ADR fixes only
that scope is established per request and never defaulted implicitly.

**Async authorization.** A bearer token authorizes a request at *request time*,
but long-running work runs as durable jobs ([0008](0008-async-worker-tier-job-queue.md))
that outlive the token. **All authorization decisions — RBAC and the
destructive-op gate ([0006](0006-oidc-rbac-attribution.md)) — are evaluated at
admission, under the caller's live token.** A job records its authorizing
`(principal, agent_session, project)` and the decided scope; the worker then
executes the already-authorized steps under a service-scoped internal grant,
**not** the caller's token, and performs no fresh authorization at execution time.
Consequently a token revoked/expired or a role downgraded *after* admission does
not retroactively authorize or de-authorize an in-flight job — stopping in-flight
work is the reconciler's responsibility (lease expiry, see the spec's
"Reconciliation & teardown"), not token expiry or role re-evaluation.

## Consequences

- Requires an external OIDC identity provider as a deployment dependency; the
  service stores no passwords.
- The `(principal, agent_session)` pair threads into authz, audit, and accounting
  uniformly (see [0006](0006-oidc-rbac-attribution.md)).
- Token validation is stateless against a cached JWKS. Request-time revocation
  relies on short token lifetimes (and introspection if later required);
  in-flight job revocation is handled by lease expiry in the reconciler, not by
  token expiry (see "Async authorization" above).
- IdP dependency is on the critical path: a stale JWKS cache during key rotation,
  or an unreachable IdP, must **fail closed** (reject new requests) rather than
  admit unvalidated tokens; already-admitted jobs continue under their internal
  grant.
- Public exposure makes TLS termination and rate limiting operational
  requirements (audience/issuer validation is a Decision-level invariant, above,
  not merely operational).
- The concrete server framework and how bearer validation integrates are pinned
  in [0010](0010-fastmcp-framework-auth.md).

## Alternatives considered

- **stdio (the PoC transport).** Rejected by core decision #2: single local user,
  no remote multi-tenancy.
- **Static API keys.** Rejected: no SSO, weak rotation, and no on-behalf-of agent
  attribution.
- **mTLS only.** Rejected: authenticates a client certificate, not a human
  principal acting through an agent, and carries no OIDC role mapping.
