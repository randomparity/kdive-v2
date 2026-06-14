# ADR 0108 — Helm demo OIDC role claims (RBAC-testable bundled issuer)

- **Status:** Proposed
- **Date:** 2026-06-13
- **Depends on:** [ADR-0088](0088-deployment-packaging.md) (the Helm chart, the
  `bundledBackends` demo path, the mock-OIDC Deployment),
  [ADR-0006](0006-oidc-rbac-attribution.md) / [ADR-0020](0020-rbac-audit-gate-implementation.md) /
  [ADR-0043](0043-platform-scoped-rbac-tier.md) (the claim-driven RBAC contract this
  emits tokens for).
- **Spec:** [`../superpowers/specs/2026-06-13-helm-demo-oidc-role-claims.md`](../archive/superpowers/specs/2026-06-13-helm-demo-oidc-role-claims.md)
- **Issue:** [#369](https://github.com/randomparity/kdive/issues/369).

## Context

The bundled demo mock-OIDC issuer (`templates/demo/oidc.yaml`, rendered only on the
`bundledBackends` path) hardcodes its `JSON_CONFIG` to mint tokens carrying exactly
`sub=kdive-demo` and `aud=["kdive"]`. Those tokens authenticate but carry no
authorization grant. kdive RBAC is claim-driven: `context_from_claims` reads `projects`,
`roles` (`{project: role}`), and `platform_roles` (a flat array); `require_role` /
`require_platform_role` gate every privileged tool. With the hardcoded no-grant claim
set, every privileged operation denies, so a stock demo deploy cannot exercise the
RBAC/authz surface at all. The claim set is baked into the template, not a value, so an
operator must fork the chart to change it (the coverage campaign hand-edited it live —
finding F2 / D2).

## Decision

### 1. The demo claim set is a Helm value, defaulted to a full RBAC grant

Add `demo.oidc.claims` to `values.yaml`. The OIDC Deployment template serializes it into
the issuer's `JSON_CONFIG` claims object instead of hardcoding the claim set. The default
grants a maximally-testable token: `admin` on the seeded `demo` project plus all three
platform roles, with `demo` listed in `projects`:

```yaml
demo:
  oidc:
    claims:
      sub: kdive-demo
      projects: ["demo"]
      roles: { demo: admin }
      platform_roles: ["platform_admin", "platform_operator", "platform_auditor"]
```

`admin` satisfies every project-role rank and the three platform roles satisfy every
`require_platform_role`, so the default token reaches every enforcement path. The project
is `demo` to match `kdive seed-demo`'s default project (so the grant lands on a project
that has a budget/quota row). An operator narrows the grant via `--set` to test denials.

### 2. `aud:["kdive"]` is a template invariant, not a value

The template always emits `"aud":["kdive"]` regardless of any operator `claims` override,
and layers the override on top of a fixed `sub` default + the pinned `aud`. This keeps the
config contract (`KDIVE_OIDC_AUDIENCE: kdive`) intact and makes a self-inflicted demo
lockout (a bad `aud` override) impossible. `test_bundled_oidc_pins_audience_kdive` stays
valid for any override.

### 3. Chart-only change

No source change in `src/kdive/security/authz/`. The parser already accepts the emitted
claim shapes; this ADR only changes what the demo issuer is configured to mint. The value
is inert on the external-backend path (the OIDC Deployment is `bundledBackends`-gated).

## Consequences

- A stock demo deploy is RBAC-testable end to end: the minted token carries a usable
  project + platform grant.
- The demo token is now *fully privileged* over project `demo`. This is acceptable and
  intended for a demo issuer that already mints a valid token for any caller and is forced
  ClusterIP-only; it is reinforced as demo-only / non-production in `values.yaml`, the
  README, and NOTES.
- Operators testing denial paths set `--set demo.oidc.claims.roles.demo=viewer` (or drop
  `platform_roles`) to mint a narrower token — the value makes the whole RBAC matrix
  reachable from a stock chart.
- A future change to the seeded demo project name must update this default in lockstep
  (the project in `roles`/`projects` must match the seeded budget/quota), noted in the
  README.

## Considered & rejected

- **Leave the claim set hardcoded, document the manual edit.** Rejected: the acceptance
  criterion is a *stock* deploy that is RBAC-testable; a manual live edit is exactly the
  D2 footgun #369 is filed against.
- **Default to a *narrow* grant (e.g. `viewer` on `demo`, no platform roles).** Rejected:
  the goal is to make the *whole* RBAC surface reachable by default; a narrow default
  leaves platform-ops and admin paths unreachable without an override, reintroducing the
  gap for the most-privileged surface. A full-grant default + documented narrowing inverts
  that: everything reachable out of the box, denials a one-line override away.
- **Expose only `roles`/`platform_roles` as values, keep `projects` hardcoded.** Rejected:
  `projects` (membership) is a third independent axis the parser reads; exposing the whole
  claims map (with `aud` pinned) is simpler and lets an operator test membership-denial
  too, with no extra template branches.
- **Make `aud` overridable like the rest of the claims.** Rejected: a wrong `aud` silently
  breaks audience verification and locks the demo out with no error at render time. Pinning
  it is a one-line fail-safe with no loss of demo utility (the audience is fixed by config).
- **Run the issuer with `interactiveLogin:true` / a real login form.** Rejected: out of
  scope; the smoke test, NOTES, and runbooks all use the no-prompt client-credentials flow,
  and a login form adds nothing to RBAC testability.
