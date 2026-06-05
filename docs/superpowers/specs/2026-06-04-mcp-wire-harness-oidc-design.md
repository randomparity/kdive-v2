# MCP-over-HTTP wire harness + OIDC token issuance — design (M1.2 sub-issue A)

**Parent spec:** [`../specs/m1.2-live-stack-e2e.md`](../../specs/m1.2-live-stack-e2e.md) ·
**Umbrella:** [`2026-06-04-live-stack-e2e-design.md`](2026-06-04-live-stack-e2e-design.md)
(sub-issue A) · **Decision:**
[ADR-0044](../../adr/0044-mcp-wire-harness-oidc-token-issuance.md) (realizes
[ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) §1/§3) · **Status:** Proposed ·
**Date:** 2026-06-04 · **Issue:** #98

## Goal

A reusable `fastmcp.Client` wrapper and an OIDC token-issuance helper, plus a thin CI-able
wire smoke test, so the live-stack spine driver (sub-issue D / #100) can drive every step as
a typed tool call over HTTP under real issuer tokens. The deliverable's load-bearing job is
to **close ADR-0042 §3's open assumption**: prove the mock-oauth2-server can mint the
nested-object `roles` claim and the `platform_roles` array claim, and that both validate
through the server's real `JWTVerifier` against the issuer's JWKS.

**Gate already cleared empirically** (recorded in [ADR-0044](../../adr/0044-mcp-wire-harness-oidc-token-issuance.md)
Context): the pinned issuer, driven through the login-form flow, puts the nested `roles`
object and the `platform_roles` array into the **access token**; the real `JWTVerifier`
accepts it and `roles_from_claims` parses it. This spec builds the harness on that proof and
turns the probe into a standing `oidc_issuer`-gated regression.

## Non-goals

- **No product code.** No `src/` change; this is a test-side harness + smoke test + two
  pytest markers. (The compose `oidc` image-tag fix — `3.1.4`→`3.0.3`, a nonexistent tag — is
  an ops fix in the same branch, not product code.)
- **No spine driver.** The phase-structured driver, the `accounting.report` call, and the VM
  path are sub-issues D/E — A only ships the harness they import and the gate that unblocks
  them.
- **No `platform_roles` *parser*.** `platform_roles_from_claims` / `PlatformRole` are
  platform-RBAC P1 (ADR-0043), not merged. A proves the claim is **minted and verified**, not
  parsed into a `RequestContext`.
- **No new *mandatory* CI infra.** The in-memory tier rides the repo's existing
  disposable-Postgres gating (skips when Docker is absent — it is *not* Docker-free, because
  its probe reads the DB); the `oidc_issuer` and `live_stack` tiers skip unless their backing
  service is up.

## Package layout

```
tests/integration/
  __init__.py                  # NEW (if absent) — make the package importable
  live_stack/
    __init__.py                # NEW
    harness.py                 # NEW — LiveStackClient + mint_token + OidcIssuer config
  test_wire_harness.py         # NEW — three-tier smoke (in-memory / oidc_issuer / live_stack)
pyproject.toml                 # + `live_stack` and `oidc_issuer` pytest markers
```

## `harness.py` surface

### `OidcIssuer` (frozen dataclass)

Resolved from env (the same vars the server reads), with explicit fields so a test can
construct one directly too:

| field | env | default | meaning |
|---|---|---|---|
| `base_url` | `KDIVE_OIDC_ISSUER` | — | issuer base, e.g. `http://localhost:8090/default` |
| `audience` | `KDIVE_OIDC_AUDIENCE` | `kdive` | the `aud` the verifier enforces |
| `client_id` | `KDIVE_OIDC_CLIENT_ID` | `kdive-test` | the OAuth client id used in the flow |

`OidcIssuer.from_env()` raises a clear error naming the missing var if `base_url` is unset
(fail fast — the live tier's preflight calls this only after deciding the stack is present).
`token_endpoint` / `authorize_endpoint` / `jwks_uri` are derived properties.

### `mint_token(...) -> str`

```python
def mint_token(
    issuer: OidcIssuer,
    *,
    subject: str,
    projects: Sequence[str],
    roles: Mapping[str, str],            # nested-object claim: {project: role}
    platform_roles: Sequence[str] | None = None,   # flat array claim
    agent_session: str | None = None,
) -> str: ...
```

Drives the issuer's **interactive-login authorization-code flow** (ADR-0044 §1, mechanism
proven by the Context probe):

1. POST to `{authorize_endpoint}?response_type=code&client_id=…&redirect_uri=…&scope=openid&state=…`
   — the login form has **no `action`**, so it posts back to the authorize URL with the oauth
   params on the **query string** and a body of just `username` + `claims` (a literal JSON
   object built by `_build_claims(...)`). The redirect MUST NOT be followed (the helper uses a
   no-redirect opener) so the `code` is read from the 302 `Location`.
2. POST `grant_type=authorization_code` + `code` + `redirect_uri` + `client_id` to
   `token_endpoint`; return `access_token` from the JSON response.

`_build_claims(...)` is a pure, network-free helper carrying `sub`/`aud`/`projects`/`roles`/
`platform_roles`/`agent_session`, so the nested-object `roles` and array `platform_roles`
shapes are unit-asserted directly. `platform_roles=None` omits the claim (a per-project-only
token); `platform_roles=[]` mints an empty array (carries the claim, grants nothing) — both
exercised so the omit-vs-empty distinction is pinned. The `aud` in `claims` is set to the
issuer's audience so the minted token targets the verifier.

### `LiveStackClient`

```python
class LiveStackClient:
    def __init__(self, client: fastmcp.Client) -> None: ...
    @classmethod
    def over_http(cls, base_url: str, token: str) -> Self: ...   # streamable HTTP + bearer
    async def list_tools(self) -> list[str]: ...                 # tool names
    async def call_tool(self, name: str, **args) -> ToolResponse | list[ToolResponse]: ...
```

**Envelope-parsing contract (fastmcp 3.4.0, verified by probe).** `Client.call_tool` returns a
`CallToolResult`. Its **`.data`** attribute is **already deserialized** by FastMCP into a
pydantic model generated from the tool's output schema (class `Root`) — **not** the project's
`ToolResponse`, **not** a raw dict. For a scalar tool `.data` is one such model; for a
`list[ToolResponse]` tool (only `resources.list` here) `.data` is a **`list`** of them.
`call_tool` converts each item to the project envelope with
`ToolResponse.model_validate(item.model_dump())`, returning a single `ToolResponse` when
`.data` is a model and `list[ToolResponse]` when it is a list. Discrimination is **by
`isinstance(result.data, list)`** — no per-tool table. (The raw-dict alternative
`CallToolResult.structured_content` wraps list results as `{"result": [...]}` while `.data`
gives the bare list; `.data` is used to avoid that asymmetry.) A pinning test asserts the
concrete shape (`resources.list` → `.data` is a list; a scalar tool → `.data` is a model) so a
future fastmcp change fails loudly, and D imports a defined contract, not an inferred one. The
constructor takes an already-built `fastmcp.Client` so the in-memory tier injects a client over
`build_app(...)` and the live tier uses `over_http`.

## Smoke test — three tiers

All tiers, where they run: connect → `list_tools` (assert the M0/M1 tool surface is present) →
one read-only `resources.list` call **per role** (`viewer`/`operator`/`admin`) plus one under
a `platform_auditor` token. `resources.list` needs only an authenticated context (no RBAC
scoping, no seeded rows), so it returns a well-formed envelope for every role against an empty
**but migrated** DB (ADR-0044 §3). No VM, no domain state.

### In-memory tier (no marker; default suite, `pull_request`)

- In-memory `fastmcp.Client` over `build_app(pool, verifier=<local-keypair JWTVerifier>)`.
  `resources.list` executes `SELECT * FROM resources` through the pool, so the tier **requires
  a migrated Postgres**: it uses the repo's disposable-Postgres fixture (`migrated_url`,
  re-exported in `tests.mcp.conftest`) and **skips cleanly when Docker is absent**, exactly as
  the other DB-backed MCP tests do (`KDIVE_REQUIRE_DOCKER=1` turns the skip into a failure in
  CI). It is "CI-able" in the same sense those are — *not* Docker-free.
- Tokens come from the in-process `mint` helper signing the same claim shapes; the injected
  verifier validates them (the ADR-0035 §3 path). Exercises the `LiveStackClient`/envelope
  seam and the per-role probe **without** the real JWKS, issuer, or HTTP transport.
- **Claim-shape unit assertions run here** (no network): `_build_claims` produces the
  nested-object `roles` and array `platform_roles`, and the in-process-minted token decodes to
  exactly those shapes. A `pull_request`-CI regression guard on the claim *shape* (the *issuer*
  gate lives in the next tier).

### Issuer-only tier (marker `oidc_issuer`) — **the executable claim-shape gate**

- Needs only the `oidc` container up (`docker compose up -d oidc`): no kdive server, no
  Postgres, no VM. Preflight `pytest.skip`s with an actionable reason unless the issuer
  (`KDIVE_OIDC_ISSUER` / the discovery + JWKS endpoints) is reachable.
- `mint_token` obtains each token **from the real issuer**; the test verifies it through a real
  `JWTVerifier` built from the issuer env against the **live JWKS**, asserting:
  - the token verifies (signature + `iss` + `aud`) → an `AccessToken`;
  - the verified `roles` claim is the expected nested object **and** `roles_from_claims` parses
    it to the expected `{project: Role}` map (the real parser, which ships);
  - the verified `platform_roles` claim is the expected flat array (minted + verified; parser
    is P1, out of scope per ADR-0044 §2);
  - a `JWTVerifier` built for the **wrong audience** rejects the token (the verifier enforces).
- This turns the manual Context probe into a standing regression at a low infra bar; a CI job
  may opt in by standing up the one container.

### `live_stack` tier (marker `live_stack`; skipped on `pull_request`)

- Preflight `pytest.skip`s unless `KDIVE_STACK_BASE_URL` and the `KDIVE_OIDC_*` issuer env are
  present and the issuer's JWKS is reachable.
- `mint_token` obtains each token from the real issuer; the **host-run server** (behind
  `KDIVE_STACK_BASE_URL`) validates them through its configured `JWTVerifier` against the live
  JWKS as each `call_tool` runs **over HTTP**. The only tier with the real transport + server
  startup + JWKS path on one wire — the shape D drives.

## Edges covered (TDD)

- `_build_claims`: nested `roles` object shape; `platform_roles` present / `None` (omitted) /
  `[]` (empty array); `agent_session` present / absent; `projects` empty.
- `mint_token` over the live issuer: a token round-trips through `JWTVerifier`; a token minted
  for the **wrong audience** is rejected by the verifier (negative — proves the verifier is
  actually enforcing, not rubber-stamping).
- `roles_from_claims` on the verified nested object → the expected map; a malformed `roles`
  value (non-string) → `AuthError` (the existing fail-closed path, asserted on issuer-minted
  claims).
- `LiveStackClient.call_tool` parses scalar (`.data` is a model) and list (`.data` is a list of
  models) envelopes into `ToolResponse`; a pinning test asserts `.data`'s concrete type;
  `list_tools` returns the expected M0/M1 names.
- Each tier's preflight skips cleanly (no stack / no issuer / no Docker) and does not error.

## Acceptance (issue #98)

- In-memory tier passes on `pull_request` (no server process, no issuer; rides the repo's
  disposable-Postgres gating — skips when Docker absent, fails when `KDIVE_REQUIRE_DOCKER=1`).
- `oidc_issuer` tier (the gate): with just the issuer container up, the issuer mints the
  nested-object `roles` claim and the `platform_roles` array claim **into the access token**;
  all tokens validate through a real `JWTVerifier` against the live JWKS, `roles_from_claims`
  parses the nested object, and a wrong-audience verifier rejects. Confirms ADR-0042 §3's open
  assumption — **already cleared empirically** (Goal / ADR-0044 Context), now a standing test.
- `live_stack` tier: the same claims validate through the **host-run server's** verifier over
  HTTP. Passes against a locally-run stack.
- `harness.py` is importable by sub-issue D.
- The escape hatch (redesign acquisition if the claim shape cannot be minted) was **not**
  needed — the login-form `claims` flow works; recorded in the PR.

## Self-review (coverage)

- Wire client wrapper returning parsed envelopes, `.data` contract pinned → `LiveStackClient`;
  ADR-0044 §3. ✓
- Nested-object `roles` + array `platform_roles` mint → `mint_token`/`_build_claims`;
  ADR-0042 §3, ADR-0044 §2. ✓
- Validate through the **real** `JWTVerifier`/JWKS — proven empirically, standing as the
  `oidc_issuer` tier → ADR-0044 §2/§3, Context. ✓
- In-memory + `oidc_issuer` + `live_stack` three-tier smoke → ADR-0044 §3. ✓
- New `live_stack` + `oidc_issuer` markers distinct from `live_vm` → `pyproject.toml`;
  ADR-0044 §4. ✓
- Importable by D → package layout under `tests/integration/live_stack/`. ✓
- In-memory tier needs migrated Postgres (not Docker-free) — false "no Docker" claim removed →
  In-memory tier; ADR-0044 §3. ✓
- No product code / no spine driver / no `platform_roles` parser → Non-goals. ✓
