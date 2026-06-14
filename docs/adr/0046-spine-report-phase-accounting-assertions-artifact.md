# ADR 0046 — Spine `report` phase: accounting assertions + report artifact (M1.2, refines 0042 §6)

- **Status:** Proposed
- **Date:** 2026-06-05
- **Refines:** [ADR-0042](0042-live-stack-e2e-mcp-http.md) §4 (the spine's phase ordering ends
  at `report`) and §6 (`accounting.report`'s all-projects form is gated `platform_auditor`).
  Builds on [ADR-0043](0043-platform-scoped-rbac-tier.md) (the platform-RBAC tier the tool's
  all-projects form is gated on) and [ADR-0045](0045-spine-driver-capability-grant-phase-naming.md)
  (the `phase`/`SpinePhaseError` naming contract this phase reuses).
- **Depends on:** the merged `accounting.report` tool (#97, M1.1 P2) and the merged spine
  driver (#100, sub-issue D). This ADR adds the **`report` phase** appended to that spine.
- **Spec:** [`../superpowers/specs/2026-06-05-accounting-assertions-report-artifact-design.md`](../archive/superpowers/specs/2026-06-05-accounting-assertions-report-artifact-design.md)

## Context

ADR-0042 §4 lists `report` as the spine's final phase and §6 settles that the report is a
server-side `accounting.report` tool whose all-projects form is gated `platform_auditor`.
The tool (#97) and the phase-structured spine through `release → teardown` (#100) are both
merged. What is **not** yet built is the `report` phase itself: the spine never drives
`accounting.report`, never asserts the run's spend against the ledger, and never emits a
report artifact. Issue #101 (sub-issue E) closes that gap.

It is deliberately a **separate sub-issue and commit from D**: an accounting regression
should bisect to E (the report phase + assertions), not to D (the libvirt driver). So E
**appends** the `report` phase and its assertion helpers; it does not modify D's phases.

Four decisions are open and settled here:

0. **What must be seeded so the spine can spend at all** — admission is fail-closed on a
   missing budget/quota, so the project must be metered before `allocate` or the run never
   reaches `report`.
1. **Where the phase runs** relative to the ledger's reserve/reconcile writes.
2. **How "the run's real spend" is asserted** when the spine runs a single project (the
   tool's multi-project rollup correctness is M1.1 P2's in-process job).
3. **What "written as an artifact" means** — format, location, and emission mechanism — and
   the exact shape of the project-only **denial** the wire RBAC negative asserts.

## Decision

### 0. The spine seeds the project's budget + quota out of band, so `allocate` can spend

Admission is **fail-closed on metering** (ADR-0007 §4): `_within_budget` reads `False` for a
project with no `budgets` row, and `_within_alloc_quota` reads `False` for a project with no
`quotas` row — either denies `allocations.request` with `allocation_denied` / `quota_exceeded`
and writes **no** ledger row. So a `report` phase that asserts `reserved > 0` for `_PROJECT`
has an unstated prerequisite: `_PROJECT` must be **metered before `allocate`**. The spine
does not set one (D drives only the public tools, and no admin sets a budget over the wire),
so without a seed the spine is denied at `allocate` and never reaches `report` — and even if
it did, with no budget there is no `reserve` and `reconcile` is a no-op (domain `reconcile`
returns `0` with no write when the project has no budget row), so the ledger has no `_PROJECT`
rows and the rollup omits the project entirely (the domain `report()` emits no row for a
project with no ledger rows).

E therefore seeds, **out of band** before `allocate` (mirroring D's `_grant_force_crash_scope`
direct DB write), one `budgets` row for `_PROJECT` with a `limit_kcu` large enough to admit
the spine's estimate, plus one `quotas` row with caps ≥ the spine's concurrency (1 allocation,
1 system). The budget upsert writes **`limit_kcu` only** and leaves `spent_kcu` untouched
(matching production `accounting.set_budget` / `BUDGETS.upsert`), so across re-runs of the
fixed-constant `_PROJECT` the DB-maintained `spent_kcu` running total stays consistent with
the ledger Σ (a fresh insert starts it at `0`). This is the minimal real metering admission
requires; it is a test prerequisite, not product behaviour, established up front (like the
capability-scope grant), not discovered mid-spine.

**Latent D gap:** because D's merged spine also seeds no budget/quota, D's own `allocate`
phase would be denied on real hardware. D was verified on the skip path, so this never
surfaced. E's seed makes the spine reachable for this issue; the orchestrator should track
hardening D's `allocate` prerequisite separately (E does not modify D's phases beyond
appending `report` + the shared up-front seed).

### 1. The `report` phase runs after `release` and `teardown`, so both ledger rows are committed

The ledger hits at **reserve-at-grant, reconcile-at-release** (ADR-0007 §3): the `reserved`
row lands at `allocations.request` (admission), and the `reconciled` row lands inside
`allocations.release`. A report taken before release would see only the `reserved` row and a
zero/absent `reconciled` — its variance would be meaningless. So the `report` phase runs
**after** the `release` phase. It runs after `teardown` too (the spine's last existing phase)
so the phase ordering reads `… → release → teardown → report`, matching ADR-0042 §4's "ends
at `report`" with the reconciler-driven teardown D already appends in between. `release`
completes the reconcile synchronously (it is not a queued job), so by the time `report` runs
both the `reserved` and `reconciled` ledger rows for the run's allocation are committed.

### 2. Real spend is asserted on a **windowed** single-project rollup row, cross-checked against the ledger

The spine runs one project (`_PROJECT`), and `_PROJECT` is a fixed constant whose ledger rows
**persist across repeated spine runs** (nothing deletes them). An all-time rollup would
therefore sum every prior run's spend, so "reflects *this run's* real spend" would be
unfalsifiable — the number only grows. To isolate the run, the phase captures a window
**`start`** just before the `allocate` phase — **read from the Postgres server clock**
(`SELECT now()` on `KDIVE_DATABASE_URL`), not the test-host clock — and passes
`window=[start, None]` to `accounting.report`. `ledger.ts` is `timestamptz` with
`DEFAULT now()` (the same server clock), so the window bound and every ledger timestamp share
**one** clock; there is no client/server skew that could push a reserved row before `start`
and silently drop it from the window (which would fail `reserved > 0` as a confusing "no
spend"). The window half-open-bounds `ts` to rows written **at or after** the run began, so
the rollup reflects only this run's spend.

The phase drives `accounting.report`'s **all-projects** form under a `platform_auditor` token
with that window and asserts on the `_PROJECT` row of the returned rollup:

- `reserved > 0` — the allocation reserved a positive estimate at grant (within the window).
- a `reconciled` value is present (the release wrote the reconcile credit row; for a spine
  that crashed and released, the credit is a non-zero `actual − Σreserved`).
- `variance == reconciled − reserved` — the tool's own per-row invariant, re-asserted over
  the wire.

To prove the rollup reflects this run's real spend (not just that it is internally
consistent), the phase independently sums the project's `reserved`/`reconciled` ledger
`kcu_delta` straight from Postgres **for the same window** (`ts >= start`, the same
`KDIVE_DATABASE_URL` the audit assertions use) and asserts the wire rollup's
`reserved`/`reconciled`/`variance` for `_PROJECT` **equal** the DB sums (quantized). Same
window on both sides, so the cross-check is apples-to-apples and falsifiable: a tool that
returned plausible-but-wrong numbers, or that ignored the window, would fail. Multi-project
rollup correctness and the granted-set form stay M1.1 P2's in-process tests — E asserts wire
reachability, the windowed spend cross-check, and the authorization boundary, not rollup
breadth.

### 3. The artifact is a JSON file written to a test artifact dir; the denial is an envelope, not a raise

**Artifact.** "Written as an artifact" is a **test-side** deliverable (a file on the host
running the spine), distinct from the MCP/MinIO artifact system (#1's vmcore). The phase
writes the `accounting.report` response payload — the `scope`, the `window`, the `_PROJECT`
rollup row, and the cross-project `total` — as a JSON file named `accounting-report.json`
under an artifact directory resolved from `KDIVE_ARTIFACT_DIR`. When that env is **unset** the
default is an **out-of-tree** location — a `kdive-live-stack-artifacts/` subdir of the system
temp dir (`tempfile.gettempdir()`), created if absent — **not** a path inside the repo. A
repo-local default would be walked by whole-tree tooling (prek/ruff/ty, test discovery) and
risks an accidental `git add -A` of a spend report; an out-of-tree default avoids both, and
`KDIVE_ARTIFACT_DIR` lets an operator or a future CI job redirect it explicitly. The phase
asserts the file **exists** and that its parsed content round-trips the asserted
`reserved`/`reconciled`/`variance` — so the artifact is proven non-empty and faithful, not
merely touched. CI never runs this phase (it is `live_stack`-gated and skips), so no CI
artifact wiring is implied.

**Denial shape (verified against `accounting.report`'s code).** The tool's all-projects form
calls `require_platform_role(...)`, catches the raised `AuthorizationError`, and **returns**
`ToolResponse.failure(..., ErrorCategory.AUTHORIZATION_DENIED)` — i.e. a well-formed envelope
with `status="error"` and `error_category="authorization_denied"`, **not** a raised tool
error. So the wire RBAC negative asserts the **envelope** shape (mirroring the spine's
existing `crash-rbac-negative`, which asserts `force_crash`'s `authorization_denied`
envelope), **not** the raised-`LiveStackToolError` path the `viewer` operator-op negative
uses. A project-only token (member of `_PROJECT`, no `platform_roles`) drives the all-projects
form and the phase asserts `status == "error"` and `error_category == "authorization_denied"`.

## Consequences

- The spine gains its final ADR-0042 §4 phase; the M1.2 exit's report criterion (spec §7)
  moves from "wired and skipped" to "wired and runnable on a KVM host."
- An accounting regression bisects to E's single commit, not D's driver (the bisectability
  the separate sub-issue buys).
- A durable, inspectable `accounting-report.json` is left on disk per spine run, useful for
  an operator debugging a spend discrepancy beyond the test's own assertions.
- The phase reuses the existing `_spine_preflight()` skip and the `phase`/`SpinePhaseError`
  contract; it adds no new gate and no product code. CI is unchanged (`live_stack` deselected).
- Two new out-of-band Postgres writes (the `budgets` + `quotas` seed) and one new read (the
  windowed ledger `kcu_delta` sums) are added to the helpers, alongside the audit-log reads and
  capability-scope grant D already performs against the same DB.
- The artifact lands out of tree by default, so a live run never dirties the working tree.
- A latent gap is surfaced (not fixed here): D's merged spine seeds no budget/quota, so its
  own `allocate` phase would be denied on real hardware. E's up-front seed unblocks the spine;
  hardening D's prerequisite is tracked separately by the orchestrator.

## Alternatives considered

- **Run `report` before `release`.** Simpler ordering, but the `reconciled` row does not
  exist until release, so variance would be undefined — the assertion would be vacuous.
  Rejected: the phase must observe a reconciled ledger.
- **Assert only the wire rollup's internal `variance == reconciled − reserved`, no DB
  cross-check.** Cheaper, no second DB read. Rejected: it proves the tool is self-consistent,
  not that the numbers are *this run's real spend* — a wrong-but-consistent rollup would pass.
  The DB cross-check is what makes the acceptance falsifiable.
- **Emit the report through the MCP artifact/MinIO surface (like the vmcore).** Matches "#1
  artifact" literally. Rejected: `accounting.report` is a read-only reporting tool with no
  artifact-write side; "written as an artifact" here is the driver persisting its own report
  for inspection, a test-side concern. Adding an artifact-write path to a read tool is scope
  E does not own.
- **Assert the denial as a raised `LiveStackToolError`.** What the `viewer` operator-op
  negative does. Rejected: it would not match the tool — the all-projects form *returns* an
  `authorization_denied` envelope (it catches the `AuthorizationError`); asserting a raise
  would fail against the real tool. Verified against the tool's code, not assumed.
- **Hard-code the artifact path / default it inside the repo.** Rejected: a repo-local default
  is walked by whole-tree tooling and risks an accidental commit of a spend report. An
  env-overridable dir with an **out-of-tree** default avoids both and still lets an operator or
  a future CI job redirect artifacts without editing the test.
- **Report all-time spend (no window) and assert wire == DB.** Cheaper, no captured timestamp.
  Rejected: `_PROJECT` is a fixed constant whose ledger persists across runs, so an all-time
  rollup sums every prior run — "this run's real spend" would be unfalsifiable. The window
  captured at `allocate` isolates the run.
- **Seed the budget over the wire via `accounting.set_budget` (admin token).** Possible, but it
  conflates metering setup with the report phase and needs an admin token the spine does not
  otherwise mint. Rejected for the same reason D grants the capability scope by a direct DB
  write: an out-of-band seed is the established pattern for a privileged test prerequisite.
