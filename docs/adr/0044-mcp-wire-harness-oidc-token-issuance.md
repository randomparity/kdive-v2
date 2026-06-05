# ADR 0044 — MCP-over-HTTP wire harness + OIDC token issuance (M1.2 sub-issue A)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Realizes:** [ADR-0042](0042-live-stack-e2e-mcp-http.md) §1 (wire client) and §3 (real
  tokens via the issuer's JWKS/`JWTVerifier` path) for the M1.2 epic's sub-issue A. This is
  the **light ADR** the umbrella spec called for: it records the token-acquisition mechanism
  and the harness boundary, settling the open assumption in ADR-0042 §3 before sub-issue D
  (issue #100) depends on it.
- **Builds on:** [ADR-0006](0006-oidc-rbac-attribution.md)/[ADR-0020](0020-rbac-audit-gate-implementation.md)
  (`roles` claim + `roles_from_claims`), [ADR-0010](0010-fastmcp-framework-auth.md)
  (`JWTVerifier`), and the claim shapes named by [ADR-0043](0043-platform-scoped-rbac-tier.md)
  (the `platform_roles` array claim).
- **Spec:** [`../superpowers/specs/2026-06-04-mcp-wire-harness-oidc-design.md`](../superpowers/specs/2026-06-04-mcp-wire-harness-oidc-design.md)

## Context

ADR-0042 §3 carried one open assumption that gates the whole live-stack epic: that
`navikt/mock-oauth2-server` (pinned `3.1.4` in `docker-compose.yml`) can mint the
**nested-object `roles` claim** (`{<project>: <role>}`) the server's `roles_from_claims`
parser expects — not only flat string/array claims — through its token flow, **and** the
flat **`platform_roles` array claim** ADR-0043 introduces. ADR-0042 made sub-issue A's wire
smoke test the gate: if the issuer cannot produce those shapes, A must redesign token
acquisition before sub-issue D is scheduled.

Every test today calls tool functions in-process with a hand-built `RequestContext` and an
injected, local-keypair `JWTVerifier`; the in-process `tests.mcp.conftest.mint` helper signs
those claims directly. Nothing obtains a token **from the issuer** or exercises the real
JWKS/`JWTVerifier` path end to end. Sub-issue A builds the reusable seam that does.

Two facts bound the decision:

- **`JWTVerifier` validates signature, `iss`, and `aud` only.** It does not inspect or reject
  custom claims; `roles`/`platform_roles`/`projects` are parsed *downstream* by
  `roles_from_claims` / `platform_roles_from_claims` / `context_from_claims`. So "validates
  through the real verifier" means: a token minted by the issuer, signed by the issuer's JWKS
  key, passes `JWTVerifier.verify_token` and yields back the exact claim payload.
- **`platform_roles_from_claims` / `PlatformRole` are not yet in `src/`.** They are
  platform-RBAC P1 (ADR-0043), not merged. So sub-issue A cannot route the `platform_roles`
  claim through a real *parser*; it proves the issuer **mints** that array shape and the
  **verifier accepts** it. The nested `roles` object, by contrast, **can** be routed through
  the real `roles_from_claims` parser, which already ships — so A asserts the stronger
  property there.

## Decision

### 1. Token acquisition uses the issuer's login flow with literal `claims`, not a static `JSON_CONFIG`

`mint_token(role, project, platform_roles=...)` drives the mock-oauth2-server's
**interactive-login authorization-code flow** and posts the desired claims as a literal JSON
object in the login form's `claims` field, then exchanges the returned `code` at the issuer's
`/{issuerId}/token` endpoint for a signed JWT. This is the navikt server's documented path
for **request-time arbitrary claims** (including nested objects) and needs **no** server-side
`tokenCallbacks`/`requestMappings` config file — so the compose `oidc` service stays unchanged
(ADR-0042's "reused unchanged" constraint holds) and the harness mints a different per-project
`roles` map and `platform_roles` set per call without restarting the issuer.

Rejected alternative — a static `JSON_CONFIG` with `requestMappings` mapping a request param
to a fixed claim set — cannot carry a *dynamic* nested role map (a different `{project: role}`
per call) without one mapping per shape, and would edit the compose service ADR-0042 froze.

### 2. The claims the issuer mints, and what each gate asserts

`mint_token` mints, for the per-project roles:

- `sub` (a per-role principal), `agent_session`, `projects` (the granted set), and the
  **nested-object** `roles` claim `{<project>: <role>}`.

and, for the platform-auditor token, additionally the **flat array** `platform_roles`
claim, e.g. `["platform_auditor"]`.

The **acceptance gate** (the smoke test) asserts:

- every minted token **verifies through the server's real `JWTVerifier`** against the issuer's
  live JWKS (signature + `iss` + `aud`);
- the verified `roles` claim round-trips as a nested object **and** parses cleanly through the
  real `roles_from_claims`, yielding the expected `{project: Role}` map (the stronger
  assertion, since that parser ships);
- the verified `platform_roles` claim round-trips as the expected flat array (the issuer
  *mints* it and the verifier *accepts* it). Routing it through `platform_roles_from_claims`
  is **out of scope for A** — that parser is platform-RBAC P1; A's gate is mint-and-verify,
  which is exactly what ADR-0042 §3 asked to confirm.

If the issuer cannot mint either shape, A redesigns acquisition (a claim-mapping config or a
token-exchange shim) before D is scheduled, and records it in the PR — the host-first/real-JWKS
shape of ADR-0042 §3 does not change, only A's mechanism.

### 3. The harness is a two-class, two-tier seam under `tests/integration/live_stack/`

`harness.py` exposes:

- `LiveStackClient` — a thin wrapper over `fastmcp.Client` (streamable HTTP) against
  `KDIVE_STACK_BASE_URL`, exposing `list_tools()` and `call_tool(name, **args)` that returns
  the **parsed `ToolResponse` envelope** (not the raw transport result). It is the single
  client seam sub-issue D imports to drive the spine.
- `mint_token(...)` and an `OidcIssuer` config (issuer base URL, `issuerId`, audience, the
  client id) read from the same `KDIVE_OIDC_*`/`KDIVE_STACK_*` env the server uses.

The smoke test ships in **two tiers**, matching ADR-0042's CI-able-vs-live split:

- a **CI-able in-memory tier** (`pull_request`) — an in-memory `fastmcp.Client` against
  `build_app(pool, verifier=...)` with an **injected local-keypair verifier** and the
  in-process `mint` helper (the ADR-0035 §3 token path). It exercises tool registration,
  schemas, and envelope serialization end to end **without** a server process, the real JWKS,
  or the issuer. It proves the `LiveStackClient` envelope-parsing seam and the per-role
  read-only call (`viewer`/`operator`/`admin` + a `platform_auditor` call) on the wire-shaped
  client. It does **not** prove the claim-shape gate.
- a **`live_stack` tier** (operator-run, skipped on `pull_request`) — `fastmcp.Client` over
  HTTP against a host-run `server` + Postgres + the **real issuer**, the tier that exercises
  the real JWKS/`JWTVerifier` path and **is** the claim-shape gate. It `pytest.skip`s with an
  actionable reason when `KDIVE_STACK_BASE_URL` / the issuer env are absent (the ADR-0035 §4
  idiom), so it is CI-safe.

The per-role read-only probe is `resources.list` — Discovery-plane reads require only an
authenticated context (no RBAC scoping, no project rows), so it returns a well-formed envelope
for every role against an empty database, isolating the auth/transport path from domain state.

### 4. A new `live_stack` pytest marker, distinct from `live_vm`

Registered in `pyproject.toml`. `live_vm` means "a KVM/libvirt host"; `live_stack` means "a
running server + issuer + Postgres." The smoke test's live tier carries `live_stack`; the
CI-able tier carries no marker and runs in the default suite.

## Consequences

- The open assumption in ADR-0042 §3 is closed by an executable gate, not a memo: the live
  tier mints both claim shapes and verifies them through the real path. Sub-issue D imports
  `harness.py` unchanged.
- `mint_token` depends on the issuer's interactive-login flow being enabled (the standalone
  default for the pinned image). If a future bump disables it, the login-form post breaks
  loudly in the live tier — caught by the gate, not silently.
- The CI surface grows by one always-run in-memory smoke test (no server, no Docker, no
  issuer) and one `live_stack`-gated test that skips on `pull_request` exactly as `live_vm`
  does. CI stays green with no new infra.
- The `platform_roles` claim is proven **mintable and verifiable** but not yet **parseable**
  here; platform-RBAC P1 lands the parser and the live-stack driver (D/E) routes it through
  `require_platform_role`. A's gate is the necessary precondition, scoped honestly.

## Alternatives considered

- **Static `JSON_CONFIG` / `requestMappings` claim mapping.** Rejected per §1 — cannot carry a
  dynamic per-call nested role map, and edits the frozen compose service.
- **Keep the in-process `mint` + injected verifier for every tier.** What ADR-0035 §3 does and
  what the CI-able tier reuses — but using it for the *live* tier too would bypass the JWKS/
  `JWTVerifier`/issuer path the gate exists to confirm. Rejected for the live tier only.
- **Mint the `platform_roles` token only once P1 lands.** Would couple A to the platform-RBAC
  epic and defer the gate ADR-0042 wants *now*. Rejected: mint-and-verify needs no parser, so
  A confirms the claim shape independently and unblocks D's scheduling.
- **Probe a domain read (`accounting.usage`) instead of `resources.list`.** Needs seeded
  ledger/project rows and RBAC scoping, coupling the auth smoke to domain state. Rejected for
  a read that is well-formed on an empty DB for every role.
