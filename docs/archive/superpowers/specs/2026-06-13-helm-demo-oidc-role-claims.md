# Helm demo OIDC role claims — make the bundled issuer mint RBAC-bearing tokens

- **Date:** 2026-06-13
- **Issue:** [#369](https://github.com/randomparity/kdive/issues/369)
- **ADR:** [ADR-0108](../../adr/0108-helm-demo-oidc-role-claims.md)
- **Found by:** the MCP tool coverage campaign
  (`docs/reports/mcp-coverage-campaign-2026-06-13.md`, finding F2 / D2).

## Problem

The bundled demo mock-OIDC issuer
(`deploy/helm/kdive/templates/demo/oidc.yaml`) hardcodes its `JSON_CONFIG` into the
Deployment template:

```json
{"interactiveLogin":false,"tokenCallbacks":[{"issuerId":"default",
 "requestMappings":[{"requestParam":"grant_type","match":"*",
 "claims":{"sub":"kdive-demo","aud":["kdive"]}}]}]}
```

Every minted token carries exactly `sub=kdive-demo` and `aud=["kdive"]` — and nothing
else. The token authenticates (it passes signature + audience verification) but carries
**no authorization grant**: no `projects`, no per-project `roles`, no `platform_roles`.

`kdive`'s RBAC is claim-driven (`src/kdive/security/authz/`):

- `context_from_claims` reads `projects` (array of project names the principal belongs
  to), `roles` (object `{project: role}` with `role ∈ {viewer, operator, admin}`), and
  `platform_roles` (flat array of `{platform_admin, platform_operator,
  platform_auditor}`).
- `require_role` / `require_platform_role` are the enforcement points every privileged
  tool calls. With the hardcoded claim set the principal is a member of **no** project
  and holds **no** platform role, so *every* privileged operation denies and the entire
  RBAC/authz surface is unreachable on a stock demo deploy.

The claim set is baked into the template, not exposed as a Helm value, so an operator
cannot configure it without forking the chart. During the coverage campaign the
`JSON_CONFIG` had to be hand-edited on the live cluster (recorded as D2 "revert if
restoring the pristine demo") to evaluate RBAC at all.

## Goal

A **stock** demo deploy (`bundledBackends=true demoAcknowledged=true`, or
`-f values-demo.yaml`) mints tokens that carry `projects` / `roles` / `platform_roles`,
so the RBAC surface is exercisable end to end without forking the chart. The full claim
set is a Helm value with a sane default; the default must let a caller reach every
enforcement path. The behavior stays demo-only and clearly non-production.

## Non-goals

- No change to production / external-backend deploys. The OIDC Deployment renders only
  under `bundledBackends` (`{{- if .Values.bundledBackends }}`); the value added here is
  inert on the external path, which uses an operator-provided issuer.
- No change to the claim *parsing* contract in `src/kdive/security/authz/`. This is a
  chart-only change; the parser already accepts the claim shapes we emit.
- No production hardening of the mock issuer. It remains an issuer that mints a valid
  `kdive` token for any caller (already documented), now with the demo role grant.

## Constraints (verified against the code)

- **Claim shapes are fixed by the parser** and must not drift:
  - `projects`: JSON array of non-empty strings (`context.py`). Absent ⇒ `()`.
  - `roles`: JSON object `{project: role}`; role string must be a known `Role`
    (`rbac.py:roles_from_claims`), else `AuthError` (fail closed). Absent ⇒ `{}`.
  - `platform_roles`: JSON **array** of known `PlatformRole` strings
    (`rbac.py:platform_roles_from_claims`); a bare string or object is rejected. Absent
    ⇒ `frozenset()`.
  - `aud` must include `kdive` (config `KDIVE_OIDC_AUDIENCE: kdive`, asserted today by
    `test_bundled_oidc_pins_audience_kdive`). `sub` must be a non-empty string.
  - A project granted in `roles` / referenced in `projects` is only *useful* (passes
    `require_role`, not just membership) if the project has a budget/quota row. The
    demo project seeded by `kdive seed-demo` defaults to **`demo`**
    (`__main__.py:_add_seed_demo_arguments`), so the default token's project must be
    `demo` to align the grant with the seeded budget/quota.

- **`JSON_CONFIG` shape is fixed by `mock-oauth2-server`** (navikt 3.0.3): claims live
  under `tokenCallbacks[].requestMappings[].claims`; `interactiveLogin:false` keeps the
  client-credentials/no-prompt flow the smoke test and NOTES use.

## Design

### D1. The claim set becomes a Helm value

Add `demo.oidc.claims` to `values.yaml` — a free-form map rendered verbatim into the
issuer's `JSON_CONFIG` `claims` object. The template stops hardcoding the claim set and
instead serializes `.Values.demo.oidc.claims` to JSON. `aud` stays pinned: the template
always emits `"aud":["kdive"]` regardless of the value (so a misconfigured override can
never break audience verification and silently lock the demo out), and merges the
operator-supplied claims on top of the fixed `sub`/`aud` floor.

Concretely the rendered claims object is:

```
{ "sub": <claims.sub | default "kdive-demo">,
  "aud": ["kdive"],                       # always, non-overridable
  ...all other keys from .Values.demo.oidc.claims }
```

### D2. The default exercises the full RBAC surface

Default `demo.oidc.claims` in `values.yaml` to a maximally-testable grant on the seeded
`demo` project:

```yaml
demo:
  oidc:
    claims:
      sub: kdive-demo
      projects: ["demo"]
      roles: { demo: admin }
      platform_roles: ["platform_admin", "platform_operator", "platform_auditor"]
```

Rationale: `admin` on `demo` satisfies every project-role rank
(`viewer<operator<admin`), and holding all three platform roles satisfies every
`require_platform_role` check. A caller minting the default token can therefore reach
*every* enforcement path that the demo backends support. An operator who wants to test a
**denial** narrows the grant via `--set` / an overlay (e.g. drop `platform_roles`, or
set `roles.demo=viewer`).

### D3. Demo-only, non-production framing

- The value lives under the existing `demo.*` block, which already renders only on the
  `bundledBackends` path.
- `values.yaml` carries a comment: this grants every demo token full RBAC over project
  `demo` + all platform roles; it is demo-only, the issuer already mints a valid token
  for any caller, and it must never front a real RBAC boundary.
- README "Bundled backends (demo only)" + NOTES.txt note that the demo token now carries
  the `demo`-project admin + platform-admin grant (so an operator knows the token is
  fully privileged and how to narrow it to test denials).

### D4. Audience stays an invariant, not a value

`aud:["kdive"]` is emitted unconditionally by the template even if an operator's
`claims` override sets `aud`. This preserves the config contract
(`KDIVE_OIDC_AUDIENCE: kdive`) and keeps `test_bundled_oidc_pins_audience_kdive` valid
no matter how `claims` is overridden — a one-line fail-safe that prevents a self-inflicted
demo lockout.

## Acceptance criteria (falsifiable)

1. `helm template … --set bundledBackends=true --set demoAcknowledged=true` renders an
   OIDC Deployment whose `JSON_CONFIG` `claims` object contains `"projects":["demo"]`,
   `"roles":{"demo":"admin"}`, and a `platform_roles` array with all three platform
   roles. (New render test.)
2. The same render still contains `"aud":["kdive"]` (existing test
   `test_bundled_oidc_pins_audience_kdive` stays green).
3. An operator override `--set demo.oidc.claims.roles.demo=viewer` renders
   `"roles":{"demo":"viewer"}` and still `"aud":["kdive"]`. (New render test proving the
   value is wired and `aud` is non-overridable.)
4. Every emitted `roles` value is a known `Role` and every `platform_roles` entry a known
   `PlatformRole` — i.e. the default token parses cleanly through
   `roles_from_claims` / `platform_roles_from_claims` without `AuthError`. (New unit
   test that feeds the default claim set through `context_from_claims`.)
5. `just ci` is green, including `chart-version-check`, `config-guard`, `helm lint`, and
   the helm render suite.

## Risks & mitigations

- **Lockout via a bad override** (operator sets `claims.aud` wrong, or omits `sub`):
  mitigated by D4 (aud pinned) and a `sub` default in the template.
- **Grant on an unseeded project** (token says `roles:{demo:admin}` but `demo` has no
  budget/quota): the default project matches `seed-demo`'s default; the demo deploy runs
  `seed-demo` (documented in the demo flow). If an operator changes the project, they
  must seed it — noted in README.
- **Confusion that this is production-safe:** mitigated by D3 framing and the existing
  ClusterIP-only / "mints a valid token for any caller" warnings.
