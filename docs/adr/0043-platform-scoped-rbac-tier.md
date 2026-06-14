# ADR 0043 — Platform-scoped RBAC tier (`platform_roles`)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Extends (does not supersede):** [ADR-0006](0006-oidc-rbac-attribution.md) — which
  ratified per-project roles and explicitly deferred a cross-project role as "a separate
  explicitly-granted claim"; this ADR adds that claim. Also builds on
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (`require_role`/audit/gate
  implementation) and [ADR-0037](0037-rbac-hardening-role-separation.md) (operator/admin
  separation). The per-project model is unchanged; this adds an **orthogonal scope**.
- **Unblocks:** ADR-0042 §6 and the live-stack epic's `accounting.report` sub-issue, which
  recorded an incoherent "admin, cross-project" scope; that report's scope becomes
  `platform_auditor` here.
- **Spec:** [`../superpowers/specs/2026-06-04-platform-rbac-tier-design.md`](../archive/superpowers/specs/2026-06-04-platform-rbac-tier-design.md)

## Context

kdive's RBAC is strictly per-project: a token's `roles` claim is a `{project: role}` map and
`require_role(ctx, project, role)` checks membership plus rank on **one** project
(`security/authz/rbac.py`). There is no actor that can operate across projects or on shared
infrastructure. ADR-0006 foresaw this:

> "there is no implicit global admin — a cross-project role, if ever needed, is a separate
> explicitly-granted claim."

A concrete need surfaced it. The live-stack epic wanted an `accounting.report` spanning
projects and recorded its scope as "admin, cross-project" — which the per-project model
cannot express: `admin` always means "admin **on project X**," and `accounting.usage` goes
out of its way to resolve an object's owning project and block cross-tenant reads
(`mcp/tools/accounting.py`, "foreign investigation_id cannot leak another tenant's usage").
A review of the whole surface found more operations with no home in the per-project model:

- **No cross-project read** for oversight (billing rollups, audit trail, inventory).
- **No host/resource maintenance** — `resources.*` is read-only over MCP; a host cannot be
  cordoned/drained/marked offline at runtime.
- **No audit-read tool at all** — `audit_log` is written on every transition but never
  exposed over MCP.
- **Bare `require_role` denials are unaudited** (only destructive-gate denials write a row).

This ADR adds a platform-scoped role tier to express these. It builds **only the read-only
(auditor) surface** now; the infra-operator and break-glass-admin operations are given a
defined home but deferred (YAGNI — they should be shaped against real use).

## Decision

### 1. A separate `platform_roles` claim, distinct from per-project `roles`

The token carries a new `platform_roles` claim — a **flat array of role strings**, e.g.
`["platform_auditor"]` — parsed fail-closed by a new `platform_roles_from_claims()` (unknown
value or non-array → `AuthError`, never a silent drop), mirroring `roles_from_claims`. The
per-project `roles` map and its parsing are untouched. `RequestContext` gains
`platform_roles: frozenset[PlatformRole]`. The two scopes never interact: `require_role` is
unchanged and unaware of platform roles.

### 2. Three distinct platform roles, granted independently (not a linear rank)

`PlatformRole` defines all three now for forward-compatible tokens:
`platform_admin`, `platform_operator`, `platform_auditor`. They are **independent grants**,
not a `viewer < operator < admin` rank, to preserve separation of duties: an infra operator
who can cordon hosts does **not** thereby gain read access to every project's debugging data
or billing, and an auditor who can read everything cannot mutate anything. The **one**
deliberate exception is a partial order — `platform_admin` satisfies `platform_auditor`
(break-glass mutation requires visibility of what it mutates) — while `platform_operator`
satisfies neither. `require_platform_role(ctx, role)` encodes this and is the sole
enforcement seam **for platform roles**; per-project authority — including
`accounting.report`'s granted-set form (§3) — continues to ride `require_role`.

### 3. This round builds the seam plus one read tool (`accounting.report`); the rest of the auditor suite parks with the operator/admin tools

This round delivers **P1** (the seam) and **P2** — `accounting.report` — a multi-project
usage/billing rollup + per-allocation reserved/reconciled variance, with optional
**group-by-principal** and **time-window** dimensions (per-developer / per-quarter spend;
`principal` comes from `ledger ⋈ allocations` on `allocation_id`, since the ledger has no
`principal` column). It has **two scope forms**, one per authority axis of §7:

- **granted-set** — a rollup over the caller's *own granted set* of projects, authorized **per
  project** by `require_role(ctx, p, viewer)` — **existing per-project membership, no platform
  role**. `viewer` is the floor, matching `accounting.usage`; `group_by=principal` adds no
  further role requirement, since per-developer spend is already within the caller's project
  visibility. The set **defaults to all of `ctx.projects`**; if the request explicitly
  **names** a project the caller is not a member of, the call is **rejected**
  (`authorization_denied`), not silently dropped — so a typo surfaces instead of returning a
  misleading partial rollup. A request resolving to **zero** member projects (e.g. empty
  `ctx.projects`) returns an **empty rollup, not an error**. This is the SW-dev-manager /
  per-team-FinOps path: a manager who is `viewer` on A/B/C gets a rollup over exactly A/B/C (no
  cross-tenant leak). It is **audited to `platform_audit_log` when the read spans >1 project
  or uses `group_by=principal`** (§4); a single-project rollup without principal grouping is
  **unaudited**, matching `accounting.usage`. The audit trigger is the *shape of the read*
  (multi-project aggregation or per-developer attribution), not a platform-role grant. Two
  honest limits: the `>1 project` trigger is **defense-in-depth, not a guarantee** — a member
  can decompose a rollup into N single-project ungrouped reads (each unaudited) and sum them,
  and the same totals are already obtainable unaudited via per-project `accounting.usage`, so
  the trigger records the *convenient* path, not every possible aggregation. The
  `group_by=principal` trigger, by contrast, **does hold**: per-developer attribution is the
  one genuinely new exposure and is always recorded. Fully auditing decomposed reads would
  require the deferred bare-read-audit retrofit (§4, M1.3).
- **all-projects** — a rollup spanning every project, gated `platform_auditor` (satisfied by
  `platform_admin` per §2) and **read-audited** to `platform_audit_log` (§4). This is the
  eng-management / finance / security oversight path, and the form the live-stack driver
  exercises over the wire.

The single-project read remains `accounting.usage` (`viewer`). Splitting authorization this
way keeps least privilege intact: a manager never needs an all-tenant grant to see their own
teams' spend, and the all-tenant view stays accountable. `accounting.report` is the one read
with live consumers (the manager/FinOps personas + the live-stack driver), and it unblocks the
live-stack epic.

The other two auditor reads are **deferred to the platform-operations milestone** (§5),
because their consumers (SRE forensics, security/compliance, sysadmin) pair with the
operator/admin tooling that also lands there:

- **`audit.query`** — read `audit_log`. Two forms: a project-scoped form requiring `admin`
  **on that project** (the audit trail is sensitive), and a cross-project form requiring
  `platform_auditor`.
- **`inventory.list`** — cross-project systems/allocations summary.

The role model defines `platform_auditor` fully now, so these slot in behind the seam later
without re-litigating it.

### 4. Platform reads (and platform-role denials) are audited to a **new `platform_audit_log` table**, not the per-project `audit_log`

A `platform_auditor`/`platform_admin` cross-project read must leave a trail (read-access
logging) — cross-tenant access has to be accountable. This is a **new pattern** (today only
state transitions and destructive-gate denials are audited), and it **cannot reuse the
existing `audit_log`**: that table is per-project by construction — `project`, `object_kind`,
and `object_id` are all `NOT NULL` (`db/schema/0001_init.sql`), and the writer
`audit.record()` enforces `project in ctx.projects` as a misattribution guard
(`security/audit.py`). A platform action has **no single project and often no single object**
(a cross-project rollup or audit-log query spans many or all), and a platform principal is
authorized by the `platform_roles` claim and **need not be a member of any project**, so
`ctx.projects` may be empty — `audit.record()` would reject the call.

We therefore add a dedicated **`platform_audit_log`** table and an `audit.record_platform()`
writer. The table records `(ts, principal, agent_session, platform_role, tool, scope, args_digest)`
where `scope` describes the breadth of the read (e.g. `all-projects`, or a filter expression)
rather than a single `project`/`object_id`; there is no project-membership guard, because
platform authority is project-independent. This round the audited read is `accounting.report`
(P2): the **all-projects** form always records a row, and the **granted-set** form records one
when the read spans >1 project or uses `group_by=principal` (§3). The `group_by=principal`
record is the load-bearing one — per-developer attribution is the only new exposure not
already obtainable unaudited; the `>1 project` record is defense-in-depth, since decomposed
per-project reads and existing `accounting.usage` stay unaudited until the M1.3 read-audit
retrofit. Because a granted-set read carries no platform role, the `platform_role` column is
**nullable** (it
records the platform role for an `platform_auditor`/`platform_admin` read and is null for a
member multi-project read; `scope` still records the project set). Keeping it a separate table
preserves the per-project `audit_log`'s `NOT NULL` invariants and per-tenant query shape, and
avoids a recursion wrinkle when `audit.query` later lands (M1.3): its cross-project form reads
`audit_log` while its own read-access record lands in `platform_audit_log`, so platform reads
never pollute the per-project trail they inspect. A `require_platform_role` denial is written
the same way **when the denied principal holds ≥1 `platform_role`** (an over-reach within the
platform tier — e.g. a `platform_operator` attempting the auditor read — which is the
accountability target). A denial of a principal carrying **no** `platform_roles` is *not*
recorded: on an openly-registered read tool that is the routine "no platform grant" case, and
auditing it would let any authenticated token amplify writes into `platform_audit_log`.
(The broader retrofit that audits all bare `require_role` denials in the per-project
`audit_log` remains a separate deferred hardening.)

### 4a. The boundary is symmetric: a platform role conveys **no** project-scoped read

§4 above treats one direction — a *project-only* token's denial of a platform read — as the
routine, unaudited non-grant case. The **inverse** is the load-bearing guarantee and is stated
here explicitly so it is a decision, not an artifact of the gate code: **a token on the
platform axis with no project membership (e.g. `operator-cli` carrying `platform_operator`, or
even `platform_admin`) is denied a project-scoped read** — `allocations.*`, `systems.*`,
`runs.*`, `jobs.*`, and the ledger/`accounting.usage_project` reads, all gated
`require_role(ctx, project, viewer)`.

This follows directly from §1 and §7: the two scope axes never interact, `require_role` is
"unchanged and unaware of platform roles," and a platform principal "need not be a member of any
project," so its `ctx.projects` is empty. The held platform role, including `platform_admin`, is
never consulted by a project read. **No platform role grants cross-project tenant-data read.**
Cross-project oversight has its own deliberate, read-audited doors — `accounting.report`
(all-projects), `inventory.list`, `audit.query` (§3), each gated `platform_auditor` — and a
platform principal who needs a *specific* project's data must be granted on that project like any
other member.

Two outcomes implement this, and they are distinct. The `require_role` **unit** raises the base
`AuthorizationError` at its non-member site for any non-member (`security/authz/rbac.py`). The
object-resolving read **tools**, however, *pre-empt* `require_role`: they resolve the object's
owning project, find the caller is not a member, and return a **not-found-shaped** result before
the role check — so a non-member (including a platform-only token) never reveals tenant existence
and never receives a distinct authorization-denied code. (The `authorization_denied` outcome is
reserved for the under-ranked *member* case, where the read does reach `require_role`.) The
guarantee — no cross-project tenant data — holds identically across both paths.

This rejects the common "admins can see everything" expectation **by design**: `platform_admin`
is break-glass *mutation* authority (it satisfies `platform_auditor` per §2 so it can read what
it mutates through the platform-tier reads), not an implicit reader of every project's
allocations/ledger. The boundary is enforced by `tests/security/authz/test_rbac_platform.py` (the
`require_role` unit denies a platform-only context a `viewer`-floor project read with the base
`AuthorizationError` — the non-member site, not the audited per-project rank-below) and by a
tool-level test that a platform-only token's by-id project read returns a not-found-shaped result,
not an authorization denial (consistent with §4).

### 5. Defined here, deferred to the **Platform-operations milestone (M1.3)**

The ADR fixes the role mapping for all of these so the seam is shaped for them, but they ship
in a later milestone — **M1.3, "Platform operations"** in the founding roadmap
([`../specs/top-level-design.md`](../design/top-level-design.md), the M1.x local-libvirt
feature-deepening band):

- **`platform_operator` infra tools** — `resources.set_status`/cordon, `resources.drain`,
  `ops.reconcile_now`, queue/worker control, runtime capacity/cost tuning.
- **`platform_admin` break-glass** — cross-project `systems.teardown`,
  `allocations.force_release`, destructive-gate override.
- **The remaining auditor reads** — `audit.query` and `inventory.list` (per §3), parked here
  because their consumers pair with the operator tooling.

M1.3 also carries the full bare-`require_role` denial-audit retrofit (§4). The full
`require_platform_role` enforcement and the `platform_audit_log`/`audit.record_platform()`
infrastructure land in **P1 this round**, so M1.3 adds tools, not authorization machinery.

### 6. kdive does not manage projects or grant roles — the IdP does

Both `roles` and `platform_roles` are issued by the identity provider; kdive only **reads and
enforces** them. There is no project-creation or role-granting tool, and this ADR adds none.
A "platform admin who provisions tenants" is the IdP's concern, not kdive's.

### 7. Two scope axes (tenant-data vs infra); the deployment root is out-of-band, not an RBAC role

Authorization in kdive runs along **two independent scope axes**, and the role model should
not conflate them:

- **Tenant-data axis**, scoped by **`project`** — the `project`-keyed objects (allocations,
  systems, runs, ledger, audit). Scoped reads: a single project (`viewer`), the caller's
  *granted set* of projects (a manager over their teams — expressed by existing multi-project
  membership, **no new role**), or all projects (`platform_auditor`).
- **Infra axis**, scoped by **location/zone** — the `resources`/hosts and provider registry,
  which carry **no `project` column** (shared infra). Today there is one host = one location,
  so infra authority (`platform_operator`) is effectively global; when hosts span labs/regions
  it must become **location-scoped**. To leave room for that without a rewrite, a
  `platform_roles` entry may later carry an **optional scope qualifier** (e.g.
  `platform_operator:<zone>`); only the unscoped form is used now.

**External/cloud providers (VPS, remote S3) need no role-model change.** A cloud provider is
just another fleet resource under the capability registry (ADR-0009/0022); its cost flows
through the existing `cost_class` pricing into platform reports. What they *add* and this ADR
anticipates as deferred: a **provider-credential-admin** duty (managing cloud/API/S3
credentials — heavier than the file-ref secret backend) on the infra axis, and the
location/zone scoping above (cloud regions).

**The deployment root is deliberately not modeled here.** The human or automation that runs
the `server`/`worker`/`reconciler`, sets the env (`DATABASE_URL`, `KDIVE_S3_*`, the OIDC
issuer/JWKS **trust**), runs migrations, and holds the Postgres-superuser / object-store-root /
secret-backend credentials is **outside** kdive RBAC by construction: it *is* the trust root
that configures the `JWTVerifier` every role check depends on, and its powers live at the
DB/secret/object-store layer that MCP never mediates. It can therefore bypass redaction (read
raw `sensitive` artifacts directly), bypass the audit log (direct DB writes), and impersonate
any principal (control token trust) — strictly more powerful than `platform_admin`, which is
tool-bounded and audited. It is governed by **infrastructure controls** (infra IAM, secret
management, change control, break-glass), not a `platform_roles` claim, and is recorded here
so the threat model accounts for the highest-trust, least-constrained actor.

## Consequences

- Tokens for platform principals must now carry a `platform_roles` claim; the IdP/issuer
  configuration grows that claim. Ordinary project tokens are unaffected (absent claim →
  empty set).
- The seam unblocks the live-stack epic's `accounting.report`; ADR-0042 §6 and that epic's
  umbrella spec are updated to point here and drop "admin, cross-project."
- A new read-access-audit pattern enters the codebase — a `platform_audit_log` table and an
  `audit.record_platform()` writer (a P1 migration) — with the obligation to apply it
  consistently to future platform read/mutation tools.
- `platform_operator`/`platform_admin` mutation tools have a defined home but are not yet
  callable; a later milestone builds them without re-litigating the role model.
- Separation of duties is now expressible and testable (operator ≠ data reader ≠ superuser),
  which the per-project model could not represent.

## Alternatives considered

- **Wildcard project in the existing `roles` map** (e.g. `{"*": "admin"}`). Smallest change,
  no new claim. Rejected: it conflates platform and project authority — a single mis-issued
  `*` grant becomes god-mode — and ADR-0006 specifically called for a *separate* claim.
- **A single linear platform rank** (`platform_viewer < platform_operator < platform_admin`).
  Symmetric with project roles and simpler. Rejected: it collapses separation of duties —
  `platform_operator` would subsume auditor read and `platform_admin` subsumes both — which
  defeats the point of distinguishing an infra operator from a data auditor.
- **Reuse per-project `admin` as if cross-project** (the live-stack epic's original "admin,
  cross-project"). Rejected: there is no such role; implementing it means either inventing a
  global role (this ADR, done explicitly) or bypassing `require_role`'s project scoping,
  which breaks the tenant-isolation boundary `accounting.usage` enforces.
- **Build the full matrix now** (operator + admin mutation tools too). Rejected as YAGNI: the
  infra-maintenance and break-glass tools are better shaped against real operational use; the
  load-bearing, expensive-to-change part is the role model, which this ADR settles now.
- **Leave platform reads unaudited** (match today's pattern). Rejected: cross-tenant access by
  a platform principal is exactly the access that must be accountable.
