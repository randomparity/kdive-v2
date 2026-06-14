# Plan — break-glass `ops.force_teardown` / `ops.force_release` (issue #140)

Implements M1.3 issue 6 (ADR-0062 §4, detailed-design §5). No new ADR: ADR-0062 §4 fully
settles the decision (separate `ops.*` path, full three-check bypass, guard-exempt audit
attribution, parameterized transition helper). No new migration: break-glass audits to the
existing `platform_audit_log` (migration `0005`); the `audit_log.reason` column is issue #8's
(#142), out of this lane.

## Goal

Add two `platform_admin` break-glass MCP tools that destroy a stuck cross-project object the
per-project tools cannot reach, authorized solely by `require_platform_role(PLATFORM_ADMIN)` +
a non-blank `reason` + an always-written `platform_audit_log` row, reusing the per-project
teardown/release **mechanics** but not their authorization or audit attribution.

## Scope boundary (conflict lanes — brief §"Shared conflict zones")

- New file `src/kdive/mcp/tools/ops/breakglass.py`; create `src/kdive/mcp/tools/ops/__init__.py`
  if absent. Minimal additive registration in `app.py`.
- In `lifecycle/allocations.py`: touch **only** `_transition_and_audit` and `_release_locked`
  (parameterize the audit writer). Do **not** touch `_resolve_resource` (issue #136's lane).
- No migration (issue #140 adds none).

## Prerequisite refactor — parameterize the audit writer

`_transition_and_audit` currently hard-calls the membership-guarded `audit.record(conn, ctx, …)`,
which raises `AuthError` when `event.project not in ctx.projects`. A break-glass admin is never a
member, so the reused release path must inject a guard-exempt writer.

Refactor: define a small writer callable type — `AuditWriter = Callable[[AsyncConnection,
audit.AuditEvent], Awaitable[None]]` — and thread an `audit_writer: AuditWriter` parameter through
`_release_locked` → `_transition_and_audit`. The default (per-project `allocations.release`) binds a
closure over `ctx` that calls `audit.record(conn, ctx, event)` (unchanged behavior). Break-glass
binds a closure over the admin principal that calls `audit.record_system(conn, principal=…,
event=…)` — the existing guard-exempt writer that takes no `RequestContext` and applies no
membership guard, writing the row under the platform principal against the **target's** project.

The `tool`/`transition`/`object_*` fields on the `AuditEvent` are unchanged for the per-project
path; the break-glass path passes the same event shape (the per-allocation `audit_log` rows record
the release transitions under the admin principal, in addition to the always-written
`platform_audit_log` summary row the tool writes).

`release_allocation` (the per-project tool) keeps calling `_release_locked` with the default writer
— behavior identical to today. `systems.teardown` / `allocations.release` stay unchanged.

`_release_locked` is unguarded — its two failure modes (`IllegalTransition` from an interleaving
the lock did not cover, and `CategorizedError` from `accounting.reconcile` when an **active**
allocation has no persisted `requested_vcpus`/`requested_memory_gb` to price) are caught by the
**caller** `release_allocation`, not by `_release_locked` itself. Break-glass targets exactly the
abnormal rows most likely to be mid-transition or missing a persisted size, so the force_release
tool **must** wrap `_release_locked` in the same `IllegalTransition` / `CategorizedError` backstops
and return a typed `configuration_error` / category-specific failure envelope — not leak the raw
exception. The shared backstop logic is factored into a helper
(`_release_with_backstops(pool, uid, *, project, audit_writer)`) reused by both `release_allocation`
and break-glass, so the two callers cannot drift (finding 1).

## `ops.force_release(allocation_id, reason)`

1. `require_platform_role(ctx, PLATFORM_ADMIN)` — denial raises `AuthorizationError` (no authz
   `ErrorCategory`; ADR-0020), surfaced by the dispatch boundary, never reaching the bypass.
2. Reject a blank/whitespace-only `reason` → `configuration_error` failure envelope.
3. Resolve the allocation by id (bad uuid / missing → `configuration_error`).
4. **Write the `platform_audit_log` row first, in its own committed transaction** (see "Audit
   ordering" below), so the break-glass attempt is recorded regardless of the release outcome.
5. Run the `_release_locked` mechanics **via `_release_with_backstops`** under the per-allocation
   advisory lock, injecting the guard-exempt `record_system` writer with the target allocation's
   `project` (resolved from the row, not a ctx grant). It **bypasses `assert_destructive_allowed`
   entirely** — no capability-scope / project-role / profile-opt-in check. A reconcile failure (no
   persisted size) or an interleaving returns a typed failure envelope; the `platform_audit_log`
   row from step 4 stays committed (finding 1, finding 2).

## `ops.force_teardown(system_id, reason)`

1. `require_platform_role(ctx, PLATFORM_ADMIN)` + non-blank `reason` (same as force_release).
2. Resolve the System by id; resolve its project from the row (bad uuid / missing →
   `configuration_error`).
3. **Write the `platform_audit_log` row first, in its own committed transaction** (Audit ordering).
4. Enqueue the **same** `JobKind.TEARDOWN` job (dedup key `{system_id}:teardown`, matching
   `systems.teardown`) under an authorizing context bound to the target's project. Bypasses the
   three-check gate. A terminal (`torn_down`) System returns success idempotently.

## Audit ordering — `platform_audit_log` is always written (finding 2)

The break-glass `platform_audit_log` row is the **sole** accountability mechanism for a
gate-bypassing tool (ADR-0062 Consequences), so it must be written **regardless of** whether the
release/teardown ultimately succeeds, partially fails, or hits a stale/terminal object. It is
therefore written via `audit.record_platform` in its **own** `conn.transaction()`, committed
**before** the release/teardown mechanic runs (and outside that mechanic's transaction). A
rolled-back or failed release never rolls back the audit row. The row is written only **after** the
gate (`require_platform_role`), the non-blank-`reason` check, and successful object resolution pass
— a denied or malformed call writes nothing (matching the per-project convention and the acceptance
criterion "a `platform_operator` token is denied", "a blank reason is rejected"). The per-allocation
`audit_log` transition rows the release path writes are separate and commit/roll back **with** the
release — they record what actually changed; the `platform_audit_log` row records the attempt.

## Response & error contract

- Success: `ToolResponse.success(object_id, status, …)` — released / the teardown job envelope.
- Bad input (blank reason, bad uuid, missing object): typed `configuration_error` failure.
- Authz denial: `AuthorizationError` propagates (handled at the boundary), not a failure envelope
  (matches the repo's "no authz ErrorCategory" convention, ADR-0020).

## Doc-guard updates (in-lane, additive)

`ops.force_teardown` / `ops.force_release` are destructive, so:
- add both to `_docmeta.DESTRUCTIVE_TOOLS`, register them with `_docmeta.destructive()`;
- update `tests/mcp/core/test_docmeta.py::test_destructive_tools_set_is_exactly_the_four`
  (reviewed-set pin) to include them;
- add both to `_BEHAVIOR_TESTS_BY_TOOL` in `tests/mcp/core/test_tool_docs.py`.

They do **not** call `assert_destructive_allowed`, so the gate-reacher backstop
(`test_backstop_actually_detects_the_known_gate_callers`) stays at the original four — unchanged.

After adding the tools: `just docs` (regenerate the tool reference), commit, then `just docs-check`
must pass.

## Tests (TDD; `tests/mcp/ops/test_breakglass.py`)

Driven directly against the handlers with an injected pool + `RequestContext` (repo unit contract),
against a real migrated Postgres.

1. **force_release cross-project success** — a `platform_admin` who is NOT a member of the
   allocation's project releases an `active` allocation; capability scope + profile opt-in absent
   (would fail the three-check gate). Asserts the allocation reaches `released` and exactly one
   `platform_audit_log` row was written.
2. **audit write succeeds despite non-membership, with pinned counts** — for a release from
   `active`, assert **exactly two** guard-exempt `audit_log` transition rows
   (`active->releasing`, `releasing->released`) were written via `record_system` (not `record`,
   which would have raised on non-membership), each with `principal` = the admin and `project` =
   the target project. A separate from-`granted` case asserts exactly one transition row. (finding 3)
3. **force_release blank/whitespace reason rejected** — `configuration_error`, allocation
   unchanged, no `platform_audit_log` row and no `audit_log` row.
4. **force_release platform_operator denied** — `require_platform_role` raises
   `AuthorizationError`; allocation unchanged; no `platform_audit_log` row.
5. **force_release of an active allocation with no persisted size returns a typed failure but
   STILL writes the `platform_audit_log` row** — seed an `active` allocation with
   `requested_vcpus`/`requested_memory_gb` = NULL and a budget row so `reconcile` raises
   `CONFIGURATION_ERROR`; assert the tool returns a `configuration_error` envelope (not a raw
   exception) AND a `platform_audit_log` row is present (audit-before-release ordering). (finding 1,
   finding 2)
6. **force_release of a terminal allocation** — `stale_handle` failure, but a `platform_audit_log`
   row is still written (the attempt is audited). (finding 2)
7. **force_teardown cross-project success** — non-member `platform_admin` tears down a `ready`
   System in another project; a `TEARDOWN` job is enqueued and exactly one `platform_audit_log` row
   written.
8. **force_teardown blank reason rejected** / **operator denied** — mirror 3/4 (no job, no row).
9. **force_teardown idempotent on torn_down** — success with no new job; the `platform_audit_log`
   row is still written.
10. **force_release / force_teardown bad-uuid and missing-object** — `configuration_error`; nothing
    audited (resolution fails before the audit write).

## Guardrails

`just lint` · `just type` · `just test` · `just docs` then `just docs-check` · `prek run -a`.
Green at every commit.
