# ADR 0037 — RBAC hardening: real operator/admin separation (M1)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** M1 — Allocation/accounting depth (RBAC hardening)
- **Depends on:** [ADR-0006](0006-oidc-rbac-attribution.md) (the three-role model),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (`Role`, `require_role`, the
  destructive-op gate), [ADR-0007](0007-metering-budgets-admission.md) (the new
  budget/quota administration surface this gates)
- **Refines:** the "Auth, RBAC & attribution" sections of the M0 and M1 specs

## Context

The roles exist and the machinery works: `Role` is a total rank
(`viewer < operator < admin`), `require_role(ctx, project, role)` is the enforcement
point every privileged tool calls, and the three-check destructive gate is fully
enforced even in M0 ([ADR-0020](0020-rbac-audit-gate-implementation.md)). What M0
deliberately did **not** do is *exercise the separation*: "M0's operator holds
`admin` for the project" (M0 spec, Auth section). Every M0 acceptance test runs as a
principal who is simultaneously operator and admin, so no test proves that an
`operator` is **refused** an `admin`-only operation. The role boundary is built but
unverified — and M1 introduces the first operations where the boundary actually
matters: setting a project's budget and quota (ADR-0007 decision 6) is project
administration, not lifecycle.

"RBAC hardening" in M1 is therefore **enforcement-wiring plus proof**, not new
machinery: place `require_role(admin)` on the new administration surface, confirm
`operator` cannot reach destructive ops, and add the negative tests M0 skipped.

## Decision

### 1. Project administration is `admin`-only; lifecycle is `operator`

The role-to-operation map is made explicit and tested:

| Role | May do |
|------|--------|
| `viewer` | read-only: `resources.*`, `*.get`/`*.list`, `accounting.usage`/`accounting.estimate` |
| `operator` | lifecycle: `allocations.request`/`.renew`/`.release`, `systems.provision`/`.reprovision`, `control.power on`, `runs.*`, `debug.*`, `introspect.*` |
| `admin` | everything operator does, **plus** project administration — `accounting.set_budget`, `accounting.set_quota` — **plus** the destructive-administration ops: `control.force_crash`, `control.power off`/`cycle`/`reset`, `systems.teardown` |

`accounting.set_budget` and `accounting.set_quota` call `require_role(ctx, project,
Role.ADMIN)`. Because the rank is total, `admin` still satisfies every `operator`
requirement; the change is that operations are pinned to the **lowest** sufficient
role, and the budget/quota ops are pinned to `admin`.

`systems.reprovision` is the one destructive op that stays `operator`: reprovisioning
your own granted System in place is iterating, not administering the project
([ADR-0038](0038-system-reprovision-in-place.md) §3). The destructive-op gate takes its
role factor as a per-op parameter precisely so this one op can require `operator` while
`force_crash` requires `admin`. `control.power on` brings a started System up — a
reversible lifecycle move, `operator`; the destructive power actions
(`off`/`cycle`/`reset`) tear into a running guest and require `admin`.

**Role checks bind to the *target* project, not just any project the caller is in.**
Every read and write resolves the project of the object it touches and checks
`require_project` + `require_role` against **that** project. This matters most for
`accounting.usage(investigation_id)`: it resolves the investigation's owning project and
checks `viewer` there — a `viewer` in project A cannot read project B's spend by passing
a B-owned `investigation_id` (ADR-0007 decision 6). Without per-object project resolution
the `viewer` grant would be a cross-project read bypass, so it carries its own negative
test (decision 3).

### 2. The destructive-op gate's role factor is **`admin`**, no longer collapsed

The three-check gate (capability scope ∧ role ∧ profile opt-in,
[ADR-0020](0020-rbac-audit-gate-implementation.md)) keeps all three checks; M1 makes
the **role** factor a true `admin` requirement for the destructive-**administration**
ops (`force_crash`, and — newly pinned here — `power off`/`cycle`/`reset` and
`teardown`) that an `operator` fails. The one exception is `systems.reprovision`, whose
role factor is `operator` ([ADR-0038](0038-system-reprovision-in-place.md) §3): the gate
takes the required role as a per-op parameter (defaulting to `admin`) so a single gate
expresses both. ADR-0006's broader "`operator` only where the op's profile opt-in
permits" generalization is **not** exercised in M1 — `reprovision` is the lone, named
`operator` destructive op; every other destructive op is `admin`, and the
operator-with-opt-in path for any new op is deferred until a provider needs it, rather
than shipped untested.

`power off`/`cycle`/`reset` and `teardown` are pinned by raising their existing
`require_role` factor from `operator` to `admin` — they are not routed through the
three-check capability-scope gate (no `capability_scope`/`profile_opt_in` is modeled for
them; only `force_crash` and `reprovision` are). This supersedes
[ADR-0028](0028-control-plane-power-force-crash.md) §3 ("`power` … authorized at
`operator` … not at `admin`") for the destructive power actions; `power on` stays
`operator`.

### 3. M1 test environments grant **separated** roles

The M0 convenience of one principal holding both roles ends. M1's mock OIDC issuer
mints distinct principals — a `viewer`, an `operator`, and an `admin` per test
project — so the suite can assert the boundary in **both** directions: the
`admin`/`operator` succeeds at what it should, and the lower role is **refused**
(`AuthorizationError` → the tool's `authorization_error`/`allocation_denied` mapping)
at what it should not. Every new privileged M1 tool ships with its negative test.

## Consequences

- The role boundary becomes a *verified* invariant, not just built code: an
  `operator` provably cannot set a budget, raise a quota, force-crash, power-cycle, or
  tear down — closing the M0 gap where every test ran as a de-facto admin. The
  `operator` retains the lifecycle ops it owns (`request`/`release`, `provision`,
  `reprovision`, `power on`, `runs`/`debug`/`introspect`).
- The new accounting administration surface (ADR-0007) has a single, consistent
  authorization rule from day one.
- No new RBAC machinery: `Role`, `require_role`, and the per-op-parameterized gate
  are unchanged; M1 adds call sites and tests. The change is small and bisectable.
- Deferring the operator-with-profile-opt-in destructive path keeps M1 strict and
  testable; it returns when a real provider's profile needs it. `reprovision` is the
  one named `operator` destructive op (ADR-0038 §3), expressed through the gate's
  per-op role parameter rather than a profile-conditional rule.
- This ADR supersedes [ADR-0028](0028-control-plane-power-force-crash.md) §3 for the
  destructive power actions: `power off`/`cycle`/`reset` move from `operator` to
  `admin` (`power on` and the gating mechanism are unchanged).

## Alternatives considered

- **Keep operator-holds-admin through M1.** Rejected: M1 introduces budget/quota
  administration, the first operations where conflating the roles is a real
  privilege-escalation risk (an `operator` raising their own project's budget). The
  separation must be real before the surface that needs it ships.
- **Add a fourth `billing-admin` role for budgets/quotas.** Rejected: premature
  (YAGNI). The three-role model covers M0–M1 (ADR-0006); a finer split is a separate,
  explicitly-justified change if a deployment ever needs budget administration
  without full project admin.
- **Ship the operator-with-opt-in destructive path now.** Rejected: it widens the
  gate's role factor to a conditional before any provider needs the looser rule,
  adding an untested escalation path. The strict rule is the safe default; loosen it
  on demand, with the provider that requires it.
