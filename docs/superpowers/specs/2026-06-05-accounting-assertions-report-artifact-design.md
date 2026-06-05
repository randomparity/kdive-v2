# Spine `report` phase: accounting assertions + report artifact (issue #101)

- **Status:** Draft
- **Date:** 2026-06-05
- **ADR:** [ADR-0046](../../adr/0046-spine-report-phase-accounting-assertions-artifact.md)
  (the convergence anchor; this spec elaborates its decisions into the test shape).
- **Issue:** [#101](https://github.com/randomparity/kdive/issues/101) — M1.2 sub-issue E.
- **Depends on (both merged):** `accounting.report` (#97, M1.1 P2) and the spine driver
  (#100, sub-issue D, `tests/integration/test_live_stack.py`).

## Goal

Append the **`report` phase** to the merged live-stack spine: drive `accounting.report`'s
all-projects form over the wire under a `platform_auditor` token, assert the run's
`reserved`/`reconciled`/variance reflect real spend, emit a JSON report artifact, and add a
wire RBAC negative (a project-only token is denied the all-projects form). A **separate
commit** from D for bisectability.

## Non-goals

- No product code. No change to `accounting.report` or the domain `report()`.
- No multi-project rollup or granted-set assertions (M1.1 P2's in-process tests; the spine
  runs one project).
- No new gate/marker/fixture; reuse `live_stack` + `_spine_preflight()`.
- No MCP/MinIO artifact-write path; the report artifact is a test-side file on disk.
- No CI integration; `live_stack` stays deselected on `pull_request`.

## Design

### Metering prerequisite (out-of-band seed, before `allocate`)

Admission is fail-closed on metering (ADR-0007 §4): `_within_budget` denies a project with no
`budgets` row, and `_within_alloc_quota` denies a project with no `quotas` row. So `_PROJECT`
must be metered **before** the `allocate` phase or the spine is denied and never reaches
`report` (ADR-0046 §0). Before `allocate`, the spine seeds out of band (a direct psycopg
write, mirroring D's `_grant_force_crash_scope`):

- a `budgets` row for `_PROJECT` with a large `limit_kcu` (so admission's
  `(limit − spent) ≥ estimate` always passes) and `spent_kcu = 0`,
- a `quotas` row for `_PROJECT` with `max_concurrent_allocations`/`max_concurrent_systems`
  ≥ 1 (the spine's concurrency).

Helper: `async def _seed_metering(db_url, project) -> None` (idempotent upsert, so a re-run
does not error). Seeded up front alongside the capability-scope grant.

### Placement

Append `report` after the existing final `teardown` phase, so the ordering reads
`… → release → teardown → report` (ADR-0042 §4). `allocations.release` reconciles
synchronously, so by `report` both the `reserved` (admission) and `reconciled` (release)
ledger rows for the run's allocation are committed (ADR-0046 §1).

### The `report` phase (inside `test_spine_over_the_wire`)

A window `start` is captured at the `allocate` phase — `datetime.now(UTC)` taken **before**
the run reserves anything — and threaded forward to isolate this run's spend from prior runs
of the fixed-constant `_PROJECT` (ADR-0046 §2). Within the existing `async with op, admin:`
block, after `teardown`:

1. Mint a `platform_auditor` token. The spine's `_token(...)` helper already accepts
   `platform_roles`; mint with `role="viewer"` on `_PROJECT` (so it is a project member for
   token shape) **plus** `platform_roles=["platform_auditor"]`. Principal `auditor-{_PROJECT}`.
2. Over a fresh `LiveStackClient.over_http(base_url, auditor_token)`, call
   `accounting.report` with `scope="all-projects"` and `window=[start.isoformat(), None]`.
   `_ok(...)` it (non-failure envelope).
3. Parse the envelope `data`: `rows` and `total` are JSON strings (the tool serializes them
   with `json.dumps`); decode and locate the row with `project == _PROJECT`.
4. Assert the `_PROJECT` row:
   - `Decimal(reserved) > 0`
   - `Decimal(reconciled)` is present (decodes)
   - `Decimal(variance) == Decimal(reconciled) − Decimal(reserved)`
5. **DB cross-check** (`_ledger_sums(db_url, project, start)`): sum `kcu_delta` filtered by
   `event_type` from the `ledger` table for `_PROJECT` **where `ts >= start`** (same window as
   the wire call), quantized via the domain `quantize_kcu`. Assert the wire row's
   `reserved`/`reconciled`/`variance` equal the DB `reserved`/`reconciled`/`reconciled −
   reserved`. Same window on both sides → apples-to-apples; this proves "this run's real spend."
6. **Emit the artifact** (`_write_report_artifact(payload) -> Path`): write the `scope`, the
   `window`, the asserted `_PROJECT` row, and the cross-project `total` as
   `accounting-report.json` under `KDIVE_ARTIFACT_DIR`; when unset, default to an
   **out-of-tree** `kdive-live-stack-artifacts/` under `tempfile.gettempdir()` (created if
   absent — never inside the repo). Assert the file exists and its parsed content round-trips
   the asserted `reserved`/`reconciled`/`variance` for `_PROJECT`.

### The wire RBAC negative (`test_report_all_projects_denied_to_project_token`)

A standalone `live_stack`-marked test (no real system needed, like the `viewer` negative):

- `_spine_preflight()` for issuer + base_url (DB unused here).
- Mint a **project-only** token: `_token(issuer, role="viewer")` — member of `_PROJECT`, no
  `platform_roles`.
- Call `accounting.report` with `scope="all-projects"`.
- Assert the **envelope** denial (verified against the tool: it catches `AuthorizationError`
  and returns `ToolResponse.failure(..., AUTHORIZATION_DENIED)`):
  `env.status == "error"` and `env.error_category == "authorization_denied"`. This mirrors
  the spine's `crash-rbac-negative`, **not** the raised-`LiveStackToolError` viewer negative.

### New helpers (added to `test_live_stack.py`, alongside D's helpers)

- `async def _seed_metering(db_url, project) -> None` — out-of-band `budgets` + `quotas`
  upsert so admission can grant.
- `async def _ledger_sums(db_url, project, since) -> tuple[Decimal, Decimal]` — `(reserved,
  reconciled)` quantized DB sums over `ts >= since`.
- `def _report_artifact_dir() -> Path` — resolve `KDIVE_ARTIFACT_DIR` or the out-of-tree
  temp-dir default.
- `def _write_report_artifact(payload: dict) -> Path` — write + return the path.
- `def _find_project_row(rows: list[dict], project: str) -> dict` — locate the row.

## Acceptance (maps to issue #101 / spec §7)

1. The report reflects **this run's** real spend (windowed wire rollup `==` windowed DB ledger
   sums) and is written as a JSON artifact whose content is re-asserted.
2. `accounting.report` all-projects form is reachable over the wire under a `platform_auditor`
   token and denied (envelope `authorization_denied`) to a project-only token.
3. `just test-live-stack` skips cleanly with no stack; `just test` (non-live) stays green.
4. Separate commit from D; D's phases unmodified beyond appending `report` + the shared
   up-front metering seed.

## Known latent gap (flagged, not fixed here)

D's merged spine (#100) seeds no `budgets`/`quotas` row, so its own `allocate` phase would be
denied on real hardware (admission is fail-closed). D was verified on the skip path, so this
never surfaced. E's `_seed_metering` unblocks the spine for this issue; hardening D's
allocate prerequisite is for the orchestrator to track separately.

## Verification

- `just lint` / `just type` / `just test` green; zero warnings.
- `just test-live-stack` with no stack → clean skip (exit-5-tolerant, all tests preflight-skip).
- Honest disclosure: only the skip path is exercisable without a KVM host + stack.
