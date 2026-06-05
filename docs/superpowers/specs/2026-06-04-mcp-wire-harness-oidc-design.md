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

## Non-goals

- **No product code.** No `src/` change; this is a test-side harness + smoke test + one
  pytest marker.
- **No spine driver.** The phase-structured driver, the `accounting.report` call, and the VM
  path are sub-issues D/E — A only ships the harness they import and the gate that unblocks
  them.
- **No `platform_roles` *parser*.** `platform_roles_from_claims` / `PlatformRole` are
  platform-RBAC P1 (ADR-0043), not merged. A proves the claim is **minted and verified**, not
  parsed into a `RequestContext`.
- **No CI infra growth.** The CI-able tier needs no server, Docker, or issuer; the live tier
  skips on `pull_request`.

## Package layout

```
tests/integration/
  __init__.py                  # NEW (if absent) — make the package importable
  live_stack/
    __init__.py                # NEW
    harness.py                 # NEW — LiveStackClient + mint_token + OidcIssuer config
  test_wire_harness.py         # NEW — two-tier smoke test (in-memory + live_stack)
pyproject.toml                 # + `live_stack` pytest marker
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

Drives the issuer's **interactive-login authorization-code flow** (ADR-0044 §1): GET the
`authorize` endpoint to start the flow, POST the login form with a literal `claims` JSON
object carrying `sub`/`projects`/`roles`/`platform_roles`/`agent_session`, follow the
redirect to capture the `code`, then POST `grant_type=authorization_code` + `code` to the
token endpoint and return the `access_token`. The `claims` JSON is built by a small pure
`_build_claims(...)` helper (unit-testable without a network) so the nested-object `roles`
and array `platform_roles` shapes are asserted directly.

`platform_roles=None` omits the claim entirely (a per-project-only token); `platform_roles=[]`
mints an empty array (a token that carries the claim but grants no platform role) — both are
exercised so the omit-vs-empty distinction is pinned.

### `LiveStackClient`

```python
class LiveStackClient:
    def __init__(self, client: fastmcp.Client) -> None: ...
    @classmethod
    def over_http(cls, base_url: str, token: str) -> Self: ...   # streamable HTTP + bearer
    async def list_tools(self) -> list[str]: ...                 # tool names
    async def call_tool(self, name: str, **args) -> ToolResponse | list[ToolResponse]: ...
```

`call_tool` parses the FastMCP transport result back into the project's `ToolResponse`
envelope (`mcp/responses.py`) — single-object tools return a `ToolResponse`, list tools
(`resources.list`) return `list[ToolResponse]` — so D asserts against the same envelope the
in-process tests use, not raw transport JSON. The constructor takes an already-built
`fastmcp.Client` so the in-memory tier can inject a client over `build_app(...)` and the live
tier uses `over_http`.

## Smoke test — two tiers

Both tiers: connect → `list_tools` (assert the M0/M1 tool surface is present) → one read-only
`resources.list` call **per role** (`viewer`/`operator`/`admin`) plus one under a
`platform_auditor` token. `resources.list` is chosen because Discovery reads need only an
authenticated context (no RBAC scoping, no seeded rows) — well-formed on an empty DB for every
role (ADR-0044 §3). No VM, no domain state.

### CI-able in-memory tier (no marker; runs on `pull_request`)

- Build `build_app(pool, verifier=<local-keypair JWTVerifier>)`; the pool need not be open —
  `resources.list` is the only call and it reads no rows in this tier's assertion path, so the
  tier asserts **registration + envelope shape over the in-memory client**, not DB reads. (If
  `resources.list` requires a live connection, the tier seeds a disposable-Postgres fixture
  via the existing `migrated_url` conftest re-export and skips cleanly when Docker is absent,
  matching the repo's db-test idiom — decided at TDD time against the real handler.)
- Tokens come from the in-process `mint` helper signing the same claim shapes; the injected
  verifier validates them. This is the ADR-0035 §3 path — it exercises the
  `LiveStackClient`/envelope seam and the per-role probe **without** the real JWKS, issuer, or
  transport.
- **Claim-shape unit assertions run here too** (no network): `_build_claims` produces the
  nested-object `roles` and array `platform_roles`, and the in-process-minted tokens carry
  exactly those shapes (decoded from the signed JWT). This gives `pull_request` CI a
  regression guard on the claim *shape* even though the *issuer* gate is live-only.

### `live_stack` tier (marker `live_stack`; skipped on `pull_request`)

- Preflight `pytest.skip`s with an actionable reason unless `KDIVE_STACK_BASE_URL` and the
  `KDIVE_OIDC_*` issuer env are present and the issuer's JWKS is reachable.
- `mint_token` obtains each token **from the real issuer**; the server (host-run, behind
  `KDIVE_STACK_BASE_URL`) validates them through its configured `JWTVerifier` against the live
  JWKS as each `call_tool` runs over HTTP.
- **This is the gate.** It asserts, additionally to the per-role probe:
  - each token verifies through a `JWTVerifier` built from the issuer env (signature + `iss` +
    `aud`) — proving the issuer signs with the JWKS key the server trusts;
  - the verified `roles` claim is the expected nested object **and** `roles_from_claims`
    parses it to the expected `{project: Role}` map (the real parser, which ships);
  - the verified `platform_roles` claim is the expected flat array (minted + verified; the
    parser is P1, out of scope per ADR-0044 §2).

## Edges covered (TDD)

- `_build_claims`: nested `roles` object shape; `platform_roles` present / `None` (omitted) /
  `[]` (empty array); `agent_session` present / absent; `projects` empty.
- `mint_token` over the live issuer: a token round-trips through `JWTVerifier`; a token minted
  for the **wrong audience** is rejected by the verifier (negative — proves the verifier is
  actually enforcing, not rubber-stamping).
- `roles_from_claims` on the verified nested object → the expected map; a malformed `roles`
  value (non-string) → `AuthError` (the existing fail-closed path, asserted on issuer-minted
  claims).
- `LiveStackClient.call_tool` parses single-object and list envelopes; `list_tools` returns
  the expected M0/M1 names.
- Live preflight skips cleanly (no stack) and does not error.

## Acceptance (issue #98)

- CI-able tier passes on `pull_request` (no server process, no issuer).
- `live_stack` tier: the issuer mints the nested-object `roles` claim and the `platform_roles`
  array claim; all tokens validate through the server's **real** verifier over HTTP — the gate
  confirming ADR-0042 §3's open assumption. Passes against a locally-run stack.
- `harness.py` is importable by sub-issue D.
- **If the issuer cannot mint that claim shape**, redesign token acquisition before D is
  scheduled, documented in the PR.

## Self-review (coverage)

- Wire client wrapper returning parsed envelopes → `LiveStackClient`; ADR-0044 §3. ✓
- Nested-object `roles` + array `platform_roles` mint → `mint_token`/`_build_claims`;
  ADR-0042 §3, ADR-0044 §2. ✓
- Validate through the **real** `JWTVerifier`/JWKS → live tier gate; ADR-0044 §2. ✓
- CI-able in-memory tier (no server/issuer) + live `live_stack` tier → two-tier smoke;
  ADR-0044 §3. ✓
- New `live_stack` marker distinct from `live_vm` → `pyproject.toml`; ADR-0044 §4. ✓
- Importable by D → package layout under `tests/integration/live_stack/`. ✓
- Redesign-if-cannot-mint escape hatch → Acceptance; ADR-0044 §2. ✓
- No product code / no spine driver / no `platform_roles` parser → Non-goals. ✓
