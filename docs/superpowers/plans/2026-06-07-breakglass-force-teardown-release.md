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

## `ops.force_release(allocation_id, reason)`

1. `require_platform_role(ctx, PLATFORM_ADMIN)` — denial raises `AuthorizationError` (no authz
   `ErrorCategory`; ADR-0020), surfaced by the dispatch boundary, never reaching the bypass.
2. Reject a blank/whitespace-only `reason` → `configuration_error` failure envelope.
3. Resolve the allocation by id (bad uuid / missing → `configuration_error`).
4. Run the **same** `_release_locked` mechanics under the per-allocation advisory lock, injecting
   the guard-exempt `record_system` writer with the target allocation's `project` (resolved from
   the row, not a ctx grant). It **bypasses `assert_destructive_allowed` entirely** — there is no
   capability-scope / project-role / profile-opt-in check.
5. Always write one `platform_audit_log` row via `audit.record_platform` (scope encodes the
   target project + allocation id; `reason` rides the `args` digest input; `platform_role` =
   the held platform roles).

## `ops.force_teardown(system_id, reason)`

1–2. Same gate + non-blank `reason`.
3. Resolve the System by id; resolve its project from the row.
4. Enqueue the **same** `JobKind.TEARDOWN` job (dedup key `{system_id}:teardown`, matching
   `systems.teardown`) under an authorizing context bound to the target's project. Bypasses the
   three-check gate. A terminal (`torn_down`) System returns success idempotently.
5. Always write one `platform_audit_log` row (scope = target project + system id; reason in the
   args digest).

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
   (would fail the three-check gate). Asserts the allocation reaches `released` and a
   `platform_audit_log` row was written.
2. **audit write succeeds despite non-membership** — assert the per-allocation `audit_log` rows
   were written (guard-exempt `record_system`, not `record`) under the admin principal against the
   target project; `record` would have raised on non-membership.
3. **force_release blank reason rejected** — `configuration_error`, no release, no audit row.
4. **force_release platform_operator denied** — `require_platform_role` denial; no release, no
   `platform_audit_log` row.
5. **force_teardown cross-project success** — non-member `platform_admin` tears down a `ready`
   System in another project; a `TEARDOWN` job is enqueued and a `platform_audit_log` row written.
6. **force_teardown blank reason rejected** / **operator denied** — mirror 3/4.
7. **force_teardown idempotent on torn_down** — success with no new job.
8. **every successful call writes exactly one `platform_audit_log` row** (count assertion).

## Guardrails

`just lint` · `just type` · `just test` · `just docs` then `just docs-check` · `prek run -a`.
Green at every commit.
