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
- **Spec:** [`../superpowers/specs/2026-06-04-mcp-wire-harness-oidc-design.md`](../archive/superpowers/specs/2026-06-04-mcp-wire-harness-oidc-design.md)

## Context

ADR-0042 §3 carried one open assumption that gates the whole live-stack epic: that
`navikt/mock-oauth2-server` (pinned `3.0.3` in `docker-compose.yml`) can mint the
**nested-object `roles` claim** (`{<project>: <role>}`) the server's `roles_from_claims`
parser expects — not only flat string/array claims — through its token flow, **and** the
flat **`platform_roles` array claim** ADR-0043 introduces. ADR-0042 made sub-issue A's wire
smoke test the gate: if the issuer cannot produce those shapes, A must redesign token
acquisition before sub-issue D is scheduled.

Every test today calls tool functions in-process with a hand-built `RequestContext` and an
injected, local-keypair `JWTVerifier`; the in-process `tests.mcp.conftest.mint` helper signs
those claims directly. Nothing obtains a token **from the issuer** or exercises the real
JWKS/`JWTVerifier` path end to end. Sub-issue A builds the reusable seam that does.

Three facts bound the decision:

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
- **The claim-shape gate is settled empirically, not assumed.** Before writing the harness, the
  pinned issuer image was stood up alone (`docker compose up -d oidc`) and driven through the
  login-form flow below; the returned **access_token** carried the nested `roles` object
  `{"proj-a": "admin"}` and the `platform_roles` array `["platform_auditor"]` with the right
  `iss`/`aud`, the real `JWTVerifier` reading the live JWKS **accepted** it, `roles_from_claims`
  parsed the verified object to `{"proj-a": Role.ADMIN}`, and a wrong-audience verifier
  **rejected** it. ADR-0042 §3's open assumption is therefore **confirmed** — the issuer's
  login-form `claims` flow into the access token, not only the id_token. The `JSON_CONFIG`/
  token-exchange fallback is **not needed** and is recorded only as the contingency had the
  probe failed. (The probe also surfaced that the compose `oidc` tag pointed at a nonexistent
  image — `3.1.4` was never published; the latest 3.x is `3.0.3` — corrected to `3.0.3` in the
  same branch.)

## Decision

### 1. Token acquisition uses the issuer's login flow with literal `claims`, not a static `JSON_CONFIG`

`mint_token(...)` drives the mock-oauth2-server's **interactive-login authorization-code
flow**: GET `/{issuerId}/authorize?response_type=code&...` returns the login page whose form
posts back to that same authorize URL (no `action`, oauth params on the query string) with
two fields — `username` and a literal `claims` JSON object; the server 302-redirects to the
`redirect_uri` with a `code`, which is exchanged at `/{issuerId}/token`
(`grant_type=authorization_code`) for the signed access token. The empirical probe (Context)
confirms the posted `claims` — including the **nested `roles` object** and the array
`platform_roles` — land in the **access token** the verifier checks. This needs **no**
server-side `tokenCallbacks`/`requestMappings` config file, so the compose `oidc` service
stays unchanged (ADR-0042's "reused unchanged" constraint holds) and the harness mints a
different per-project `roles` map and `platform_roles` set per call without restarting the
issuer.

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

All three assertions were run by hand against the standalone issuer (Context) and held, so the
gate is met before the harness is built; the live tier (§3) is the standing regression of that
proof. The escape hatch is therefore unused: **had** the issuer been unable to mint either
shape into the access token, A would have switched acquisition to a `JSON_CONFIG`
`tokenCallbacks` mapping or a token-exchange shim before D was scheduled and recorded it in the
PR — the host-first/real-JWKS shape of ADR-0042 §3 would not change, only A's mechanism.

### 3. The harness is a two-class seam under `tests/integration/live_stack/`, exercised in three tiers

`harness.py` exposes:

- `LiveStackClient` — a thin wrapper over `fastmcp.Client`, exposing `list_tools()` (tool
  names) and `call_tool(name, **args)` that returns the **parsed `ToolResponse` envelope** (not
  the raw transport result). A `LiveStackClient.over_http(base_url, token)` classmethod builds
  the streamable-HTTP + bearer client for the live tier; the constructor also accepts an
  already-built in-memory client so the lower tiers reuse the same envelope-parsing seam D
  imports. **Envelope parsing (fastmcp 3.4.0, verified by probe):** `Client.call_tool` returns
  a `CallToolResult`; `call_tool` reads its **`.structured_content`** — a clean `dict`. A scalar
  tool's payload is the object dict directly; a `list[ToolResponse]` tool (only `resources.list`
  here) is wrapped by FastMCP as **`{"result": [<dict>, ...]}`**. So `call_tool` returns a list
  of `ToolResponse` when the payload is exactly a single `result` key holding a list, and one
  `ToolResponse` otherwise — each parsed with `ToolResponse.model_validate(...)`. (`.data` is
  **not** used: it is a FastMCP-generated plain class — `fastmcp.utilities.json_schema_type.Root`,
  not a pydantic model — so it has no `model_dump`; `.structured_content` is the dict source.) A
  pinning test asserts the concrete shape — a scalar payload is the object dict, a list payload is
  the `{"result": [...]}` wrapper — so a future fastmcp change to this surface fails loudly.
- `mint_token(...)` and an `OidcIssuer` config (issuer base URL, audience, client id) read
  from the same `KDIVE_OIDC_*`/`KDIVE_STACK_*` env the server uses.

The smoke test ships in **three tiers** — a finer split than ADR-0042's CI-able-vs-live, so the
claim-shape gate gets an automated signal that does not require the full host stack:

- a **CI-able in-memory tier** (default suite, `pull_request`, **no marker**) — an in-memory
  `fastmcp.Client` over a small probe `FastMCP` app. **Constraint (verified by probe):** the
  in-memory `FastMCPTransport` rejects `auth=` and carries **no** access token, so
  `get_access_token()` returns `None` in-process and any tool that calls `current_context()`
  (every kdive plane tool, including `resources.list`) cannot be driven through the in-memory
  client. This tier therefore proves only what the in-memory transport *can* prove: the
  `LiveStackClient` **envelope-parsing seam** (a scalar tool → one `ToolResponse`; a
  `list[ToolResponse]` tool → a list; the `.data` shape pin) and `list_tools()`, against probe
  tools that do not read auth, plus — purely in-process, no network — the **claim *shape*** via
  `_build_claims` and the in-process `mint` (the nested `roles` object and the array
  `platform_roles` decode exactly). It needs **no Postgres and no Docker** (the probe tools read
  no DB), so it always runs on `pull_request`. The **per-role read-only probe is not run here** —
  it requires real auth, which the in-memory transport lacks; it lives in the `live_stack` tier.
- an **issuer-only tier** (`oidc_issuer` marker) — stands up **only** the `oidc` container
  (`docker compose up -d oidc`, no kdive server, no Postgres, no VM), has `mint_token` obtain
  each token from the **real issuer**, and verifies it through a real `JWTVerifier` built from
  the issuer env against the **live JWKS**. **This is the executable claim-shape gate**: it
  asserts the nested `roles` object + array `platform_roles` are in the verified access token,
  `roles_from_claims` parses the object, and a wrong-audience verifier rejects. It `pytest.skip`s
  when `KDIVE_OIDC_ISSUER`/the issuer is unreachable, so it runs wherever Docker + the issuer
  are up (a CI job *can* opt in) and skips otherwise. This tier makes the gate a standing
  regression rather than a one-time manual probe, at a far lower bar than the full stack.
- a **`live_stack` tier** (marker `live_stack`, operator-run, skipped on `pull_request`) —
  `fastmcp.Client` **over HTTP** against a host-run `server` + Postgres + the real issuer:
  the only tier that puts the real transport, server startup, **and** the JWKS/`JWTVerifier`
  path on one wire — and therefore the **only** tier where the per-role read-only probe
  (`viewer`/`operator`/`admin` + a `platform_auditor` call to `resources.list`) actually
  exercises authenticated tool dispatch end to end. It `pytest.skip`s with an actionable reason
  when `KDIVE_STACK_BASE_URL` / the issuer env are absent (the ADR-0035 §4 idiom), so it is
  CI-safe.

The per-role read-only probe (in the `live_stack` tier) is `resources.list` — Discovery-plane
reads require only an authenticated context (no RBAC scoping, no project rows), so it returns a
well-formed envelope for every role against an empty database, isolating the auth/transport path
from domain state.

### 4. Two new pytest markers, distinct from `live_vm`

Registered in `pyproject.toml`. `live_vm` means "a KVM/libvirt host"; the new `oidc_issuer`
means "the mock issuer container is up" (no server, no VM); `live_stack` means "a running
server + issuer + Postgres." The issuer-only tier carries `oidc_issuer`, the wire tier carries
`live_stack`, and the in-memory tier carries no marker — it reads no DB and needs no Docker, so
it runs unconditionally in the default suite.

## Consequences

- The open assumption in ADR-0042 §3 is closed by an executable gate, not a memo, and is
  proven **before** the harness is built (Context). The issuer-only tier keeps it a standing
  regression at a low infra bar (just the `oidc` container); the live tier additionally proves
  the same claims over the real HTTP transport. Sub-issue D imports `harness.py` unchanged.
- `mint_token` depends on the issuer's interactive-login flow being enabled (the standalone
  default for the pinned image) and on the login form posting `username` + `claims` back to the
  authorize URL. If a future image bump changes either, the login-form post breaks loudly in
  the issuer-only tier — caught by the gate, not silently.
- The CI surface grows by: one **always-run, Docker-free** in-memory smoke test (probe app,
  no DB, no auth — it covers the envelope-parsing seam + claim shape only, because the
  in-memory transport carries no token); one **`oidc_issuer`-gated** test that runs only when
  the issuer container is up (a CI job can opt in with `docker compose up -d oidc`); and one
  **`live_stack`-gated** test skipped on `pull_request` exactly as `live_vm` is — the only tier
  that drives authenticated tool calls. Default `pull_request` CI gains no new mandatory infra.
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
  a read that is well-formed on an empty DB for every role (in the `live_stack` tier).
- **Drive a kdive plane tool through the in-memory client (no real server).** Rejected because
  the in-memory `FastMCPTransport` carries no access token (verified: it rejects `auth=` and
  `get_access_token()` returns `None`), so any tool calling `current_context()` cannot run that
  way. The in-memory tier covers only the auth-free envelope seam; authenticated dispatch is the
  `live_stack` tier's job.
