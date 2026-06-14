# Platform-scoped RBAC tier + auditor suite — Epic design

**Parent spec:** [`docs/specs/m1.1-platform-rbac-tier.md`](../../specs/m1.1-platform-rbac-tier.md)
(the M1.1 integration contract) · **Decisions:**
[ADR-0043](../../adr/0043-platform-scoped-rbac-tier.md) (the role-model decision this epic
realizes; extends [ADR-0006](../../adr/0006-oidc-rbac-attribution.md)) · **Status:** Proposed ·
**Date:** 2026-06-04

Umbrella spec for adding a **platform-scoped** authorization tier alongside the existing
per-project roles, plus the read-only cross-project ("auditor") tool surface. GitHub issues
are cut from this document after review. Each sub-issue gets its own spec → plan → impl cycle.

## Goal

Express authority that the per-project model cannot: acting across projects and on shared
infrastructure. **This round delivers the role-model seam** (the expensive-to-change part) plus
**one read tool — `accounting.report`** — the report with live consumers, in two scope forms:
a **granted-set** form (the SW-dev-manager / per-team-FinOps path, authorized by existing
membership, no platform grant) and an **all-projects** form (the eng-management / finance /
security oversight path + the live-stack driver, gated `platform_auditor`). The remaining
auditor reads (`audit.query`, `inventory.list`) and all infra-operator / break-glass-admin
*mutation* tools are defined in the ADR but **deferred to the Platform-operations milestone
(M1.3)** (ADR-0043 §3, §5).

## Non-goals

- **No project or role management.** kdive reads `roles`/`platform_roles` from the IdP token;
  it does not create projects or grant roles (ADR-0043 §6).
- **No infra-mutation tools this round** — cordon/drain, reconcile-now, worker/queue control,
  break-glass teardown are deferred (ADR-0043 §5).
- **No change to per-project RBAC.** `roles`, `roles_from_claims`, `require_role` are
  untouched; the platform tier is orthogonal.
- **No full bare-`require_role` denial-audit retrofit** — only the new platform surface audits
  its denials; the broader retrofit is a separate hardening.

## The role model (decided)

- New token claim `platform_roles` — a flat array of strings — parsed fail-closed by
  `platform_roles_from_claims()`. Absent claim → empty set.
- `PlatformRole`: `platform_admin`, `platform_operator`, `platform_auditor` (all defined now;
  only auditor exercised this round).
- **Independent grants, not a rank**, with one partial-order exception: `platform_admin`
  satisfies `platform_auditor`; `platform_operator` satisfies neither.
- `RequestContext.platform_roles: frozenset[PlatformRole]`; `require_platform_role(ctx, role)`
  is the only enforcement seam. No interaction with `require_role`.

## Personas & access requirements

The role model is anchored in jobs-to-be-done. Scope legend: **own** (my work) · **project**
(one) · **granted-set** (the projects I'm granted — e.g. a manager's teams, via existing
multi-project membership) · **fleet** (infra/hosts) · **platform** (all projects' tenant
data). Tenant-data is scoped by `project`; infra by location/zone — two independent axes
(ADR-0043 §7).

| Job | Needs to answer / do | Scope | R/W | Maps to role | Tool support |
|---|---|---|---|---|---|
| Developer / debug agent | provision or pick a system; build/boot/debug; own run's cost | own / project | RW | project `operator`+`viewer` | exists; **+ `systems.list`** (project, new) |
| SW dev manager | systems my devs use (CAPEX); spend **per developer**, per investigation, per quarter | granted-set | R | project `viewer` on their projects (**no new role**) | **`accounting.report` granted-set form** (per-developer/per-quarter rollup over the caller's member projects, no platform grant) — new |
| Project manager | investigation/run status, who's doing what, project spend vs budget | project | R | project `viewer`/`admin` | `accounting.usage` exists; **`investigations.list` missing** |
| System administrator | host health/capacity, systems-per-host, cordon/drain/maintenance | fleet | RW | `platform_operator` | `resources.list/describe` exist; **set_status/drain deferred**; `inventory.list` new |
| SRE engineer | queue/worker/provider health, force reconcile, incident forensics | fleet / platform | RW | `platform_operator` (+`platform_auditor`) | **`ops.*` deferred**; **`audit.query` new**; `inventory.list` new |
| Eng management | utilization & cost trends across all projects, adoption, CAPEX | platform | R | `platform_auditor` | **`accounting.report` (cross-project, time-series) new** |
| Finance / FinOps | chargeback per project/cost-center, budget vs actual, quarterly | platform | R | `platform_auditor` | **`accounting.report` + budgets read, new** |
| Security / compliance | who did what, denied attempts, destructive ops, cross-tenant access | platform | R | `platform_auditor` | **`audit.query` + `platform_audit_log` new** |
| Platform optimization agent | reap idle systems, rebalance, cost-optimize across projects | platform | RW | `platform_operator` (+auditor reads) | reads `inventory.list`/`accounting.report` new; **mutations deferred** |
| **Deployment root** (service operator) | run server/worker/reconciler; OIDC/DB/secret/object-store config; migrations | deployment (below MCP) | RW | **none — out-of-band, not an RBAC role** | governed by infra controls; can bypass redaction/audit/RBAC (ADR-0043 §7) |

Implications (folded into ADR-0043): **three report scopes** (single-project / granted-set /
all-projects) — single-project is the existing `accounting.usage` (`viewer`); granted-set and
all-projects are the two forms of `accounting.report` (P2), and the **granted-set form needs
no platform grant** so the manager sees their own teams under existing membership while the
all-projects form is `platform_auditor`-gated and audited. **Per-developer spend** is
derivable by `ledger ⋈ allocations` on `allocation_id` (the ledger has no `principal` column;
`allocations.principal` supplies it, `ledger_allocation_id_idx` covers the join) via a
group-by-principal + time-window report, available in both forms. The jobs are
**overwhelmingly read-only** (only sysadmin / SRE / optimization-agent need infra mutations,
which stay deferred); and the **deployment root** is the highest-trust actor and is
deliberately outside RBAC.

## Tool surface

| Tool | Form | Required role | Audited? | When |
|---|---|---|---|---|
| `accounting.report` | **granted-set**: rollup over caller's member projects + per-allocation variance (opt. group-by-principal/window) | per-project `viewer` membership (**no platform role**) | iff >1 project or `group_by=principal` | **this round (P2)** |
| `accounting.report` | **all-projects**: same rollup spanning every project | `platform_auditor` | read audited | **this round (P2)** |
| `audit.query` | project-scoped (one project's audit log) | `admin` **on that project** | n/a (project read) | **M1.3 (P3)** |
| `audit.query` | cross-project | `platform_auditor` | read audited | **M1.3 (P3)** |
| `inventory.list` | cross-project systems/allocations summary | `platform_auditor` | read audited | **M1.3 (P4)** |

Per-project reads are unchanged: `accounting.usage` stays `viewer`.

## Decomposition (sub-issues)

P1 is foundational. **This round ships P1 + P2.** P3 and P4 are **deferred to the
Platform-operations milestone (M1.3)** alongside the operator/admin tools (ADR-0043 §3, §5);
they remain specified here so M1.3 inherits ready acceptance criteria.

### P1 — Platform RBAC seam  *(spec + the ADR-0043 anchor)*
`PlatformRole` enum; `platform_roles_from_claims()` (fail-closed); `RequestContext.platform_roles`;
`require_platform_role()` with the `admin ⊇ auditor` partial order; and the **platform-audit**
path (ADR-0043 §4). The last item is **not** a thin helper over the existing `audit_log`: that
table's `project`/`object_kind`/`object_id` are `NOT NULL` and `audit.record()` enforces
`project in ctx.projects`, which a project-independent platform action cannot satisfy. P1 adds
a forward-only migration for a new **`platform_audit_log`** table (`ts, principal,
agent_session, platform_role, tool, scope, args_digest`) and an `audit.record_platform()`
writer with no project-membership guard, used for both successful platform reads and
`require_platform_role` denials. **Depends on:** nothing. **Acceptance:** unit tests cover
claim parsing (valid / unknown role / non-array / absent → fail-closed) and
`require_platform_role` for each role and the partial order; the migration creates
`platform_audit_log`; `audit.record_platform()` writes a row for a principal with **empty
`ctx.projects`** (proving the membership guard is bypassed) and for a denial; no tool yet
calls it beyond a test harness.

### P2 — `accounting.report` on the seam  *(spec + ADR section)*
The multi-project usage/billing rollup + per-allocation reserved/reconciled variance tool,
with optional group-by-principal / time-window dimensions, in **two scope forms** (ADR-0043
§3): a **granted-set** form over the caller's member projects (per-project `viewer`
membership, no platform role; audited iff >1 project or `group_by=principal`) and an
**all-projects** form gated `platform_auditor` and always read audited. **This replaces the live-stack epic's sub-issue B** ("accounting.report,
admin/cross-project"), whose scope was incoherent; the live-stack epic's ADR-0042 §6 and
umbrella spec are updated to depend on P1+P2. **Depends on:** P1. **Acceptance:** (a) a
`platform_auditor` token gets an all-projects rollup spanning ≥2 projects with correct
per-project and total variance against a hand-computed ledger, and the read writes a
`platform_audit_log` row; (b) a member of projects A+B (no platform role) gets a granted-set
rollup over exactly A+B (authorized per project by `require_role(…, viewer)`); a non-member
project explicitly named in the request is **rejected** (`authorization_denied`), not silently
dropped; a request resolving to zero member projects returns an **empty rollup, not an error**;
the granted-set read writes a `platform_audit_log` row (with `platform_role` null) **iff** it
spans >1 project or uses `group_by=principal`, and **no** row for a single-project ungrouped
read; (c) `group_by=principal` over a window
returns correct per-principal totals via `ledger ⋈ allocations`; (d) a project-only token is
**denied the all-projects form**. No VM needed (disposable Postgres).

### P3 — `audit.query`  *(deferred to M1.3)*
Read the `audit_log`. Project-scoped form requires `admin` on the named project; cross-project
form requires `platform_auditor` (and audits the read). First MCP exposure of the audit trail.
**Depends on:** P1 (the seam + `platform_audit_log`, both shipped this round). **Acceptance:** project form returns only that project's rows and is denied
to non-admins; cross-project form spans projects under `platform_auditor` and is denied to a
project-only token; filtering (by object/principal/time window) returns the expected subset;
cross-project read writes an audit row.

### P4 — `inventory.list`  *(deferred to M1.3)*
Cross-project summary of systems/allocations (state, project, owning principal, age), gated
`platform_auditor`, read audited. **Depends on:** P1 (shipped this round). **Acceptance:** returns rows across ≥2
projects for `platform_auditor`; denied to project-only tokens; reflects live state against a
seeded fixture; read audited.

### Platform-operations milestone (M1.3) — defined here, built later
Grouped because their consumers (SRE, security/compliance, sysadmin, optimization agent) pair
with the operator tooling: **P3 `audit.query`** and **P4 `inventory.list`** (above);
`platform_operator` infra suite (`resources.set_status`/cordon, `resources.drain`,
`ops.reconcile_now`, queue/worker control, runtime capacity/cost tuning); `platform_admin`
break-glass (cross-project `systems.teardown`, `allocations.force_release`, gate override); and
the full bare-`require_role` denial-audit retrofit. All sit behind P1's seam, so M1.3 adds tools,
not authorization machinery. (Milestone number to confirm in the tracker.)

## Dependency graph

```
  this round:   P1 ─► P2        (P2 also resolves live-stack sub-issue B)
  M1.3 (later):   P1 ─┬─► P3 (audit.query)
                    ├─► P4 (inventory.list)
                    └─► operator/admin suites + denial-audit retrofit
```

## Cross-epic impact (live-stack E2E)

The live-stack epic ([ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md), umbrella spec
`2026-06-04-live-stack-e2e-design.md`) recorded `accounting.report` as "admin, cross-project."
That is superseded here:

- ADR-0042 §6 and the live-stack umbrella spec are updated to reference ADR-0043; the report
  tool is `platform_auditor`-gated and delivered as **P2**, not live-stack sub-issue B.
- The live-stack spine's `report` phase uses a `platform_auditor` token (the wire test then
  exercises the platform claim over HTTP), alongside the existing `viewer` `accounting.usage`.
- Live-stack sub-issue D (the driver) gains a dependency on P1+P2.

**Merge-order invariant:** P1 (the platform-RBAC seam) is foundational and must merge to
`main` before the live-stack `accounting.report` consumer (sub-issue D), so the cross-epic
references to ADR-0043 resolve. (These specs were authored as a stack and squashed into a
single doc commit; the invariant, not the branch topology, is what the implementation epics
must honour.)

## Testing

- **Seam (P1):** pure unit tests on parsing + `require_platform_role` + the audit helpers;
  property-style coverage of the partial order (admin⊇auditor, operator⊥both) and fail-closed
  parsing.
- **`accounting.report` (P2, this round):** in-process tests against disposable Postgres with
  multi-project fixtures and both membership-only and platform-role `RequestContext`s,
  asserting: the **all-projects** rollup (≥2 projects) under `platform_auditor` with a
  `platform_audit_log` row (ADR-0043 §4), not a per-project `audit_log` row; the
  **granted-set** rollup over exactly the caller's member projects (authorized per project by
  `require_role(…, viewer)`), with a named non-member project **rejected** (not dropped), a
  zero-project resolution returning an empty rollup, and a `platform_audit_log` row written
  **iff** the read spans >1 project or uses `group_by=principal` (single-project ungrouped →
  none); **group-by-principal/window**
  totals via `ledger ⋈ allocations`; and **separation-of-duties negatives** (project-only
  token denied the all-projects form; `platform_operator` denied the auditor read). (P3/P4
  carry the same test shape in M1.3.)
- **Wire (deferred to live-stack D):** a real `platform_auditor` token from the OIDC issuer
  drives `accounting.report` over HTTP.

## Risks

- **IdP claim shape** — the issuer must mint the `platform_roles` array claim; same
  external-tool assumption as the live-stack OIDC work, confirmed in the live-stack harness
  (its sub-issue A) before the wire test depends on it.
- **Read-access audit volume** — auditing every platform read is a new write path; cheap now,
  but worth watching if a platform auditor polls inventory frequently.
- **ADR numbering / merge order** — the ADR index is ordered 0042 → 0043 and the platform-RBAC
  seam (P1) is foundational, so its implementation must merge before the live-stack
  `accounting.report` consumer that depends on it (see the merge-order invariant above).
