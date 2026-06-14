# ADR 0006 — OIDC/SSO + RBAC with (principal, agent_session) attribution

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #6 in [`../specs/top-level-design.md`](../design/top-level-design.md)

## Context

Identity moves from the PoC's implicit local user to OIDC/SSO with RBAC, and every
state transition and destructive op is attributed to `(principal, agent_session)`
for on-behalf-of agent accountability. See the spec's "Cross-cutting concerns"
(Audit log) and "MCP tool surface".

## Decision

Authentication is **OIDC/SSO**; authorization is **RBAC** with a small fixed role
set — `viewer` (read-only), `operator` (lifecycle ops: allocate, provision, build,
install, debug), `admin` (destructive ops and project administration). **Roles are
scoped to a project and asserted by the IdP** (the memberships
[0002](0002-multi-user-mcp-http.md) validates), never self-assigned; **there is no
implicit global admin** — a cross-project role, if ever needed, is a separate
explicitly-granted claim. A **destructive operation requires three independent,
all-required checks**: the granted allocation capability scope, an `admin` role
for that project (or `operator` only where the op's profile opt-in permits), and
the explicit profile/flag opt-in. Every state transition and destructive op is
attributed to `(principal, agent_session)` and written to the append-only audit
log. The agent acts **on behalf of** a principal: `principal` is the token
subject; `agent_session` is a per-session claim.

## Consequences

- Role checks compose with the destructive-op gate's other factors (capability
  scope, explicit profile/flag opt-in) — three independent, all-required checks.
- On-behalf-of attribution makes every agent action traceable to a human principal
  in audit and accounting.
- Requires role assignment management and an IdP integration; M0 ships the
  three-role set, not arbitrary attribute policy.
- Token and claim shape are pinned with the framework in
  [0010](0010-fastmcp-framework-auth.md).

## Alternatives considered

- **Service-local accounts.** Rejected by core decision #6: no SSO, no central
  identity, weak for multi-tenant.
- **ABAC / external policy engine (OPA).** Rejected: premature; the three-role
  model covers M0–M1 and a policy engine can layer on later.
- **No RBAC, authorize by allocation ownership only.** Rejected: destructive ops
  (force-crash, teardown, power) need role gating beyond resource ownership.
