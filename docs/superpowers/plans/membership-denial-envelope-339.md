# Plan — Envelope project-membership denials as `authorization_denied` (#339)

- **Date:** 2026-06-12
- **Spec:** [membership-denial-envelope](../specs/2026-06-12-membership-denial-envelope.md)
- **ADR:** [0098](../../adr/0098-membership-denial-envelope.md)
- **Branch:** `fix/authz-denial-surfacing-339`

## Outcome

Every project-authorization denial across the MCP/CLI surface maps to **exit 3** with
`error_category="authorization_denied"`, via a **single seam**: `require_project` raises a typed
`ProjectMembershipDenied` (a subclass of `AuthError`) which `DenialAuditMiddleware` catches at the
dispatch boundary and envelopes — **without** auditing (non-member = non-amplifying). No tool
handler is modified; the by-id `not_found` no-leak path is structurally untouched.

## No migration

No DDL/migration. The denial is intentionally **unaudited** (no new `audit_log` row shape), and no
schema changes. (If something forces one, use `0027+` — but it should not.)

## Steps (strict TDD — failing test first, then code, per step)

### Step 1 — `ProjectMembershipDenied` exception + `require_project` raises it

1. **Test (red):** in `tests/security/authz/test_context.py` (or `tests/mcp/core/test_auth.py`)
   assert `require_project(ctx, ungranted)` raises `ProjectMembershipDenied`, and that
   `ProjectMembershipDenied` is a subclass of `AuthError` (`issubclass`).
2. **Code (green):**
   - `src/kdive/security/authz/errors.py`: add
     `class ProjectMembershipDenied(AuthError)` with a docstring (ADR-0098).
   - `src/kdive/security/authz/context.py`: `require_project` raises
     `ProjectMembershipDenied(...)` (same message) instead of `AuthError(...)`.
   - `src/kdive/mcp/auth.py`: add `ProjectMembershipDenied` to the import + `__all__`
     (it re-exports `AuthError`; keep the surface consistent).

### Step 2 — `DenialAuditMiddleware` envelopes the membership denial (no audit)

1. **Test (red):** in `tests/mcp/core/test_denial_audit_middleware.py`, add
   `test_project_membership_denied_envelopes_without_audit`: drive `mw.on_call_tool` over a
   `call_next` raising `ProjectMembershipDenied`; assert the returned
   `ToolResponse.error_category == "authorization_denied"` **and** `await _count_audit(conn) == 0`.
   (Mirror `test_role_denied_envelope` + `test_base_authorization_error_is_not_audited`.)
2. **Code (green):** in `src/kdive/mcp/middleware.py` `DenialAuditMiddleware.on_call_tool`, add an
   `except ProjectMembershipDenied:` clause that returns
   `ToolResponse.failure(context.message.name, ErrorCategory.AUTHORIZATION_DENIED)` and does **not**
   write an audit row. Import `ProjectMembershipDenied` from `kdive.security.authz.errors`. Update
   the module/class docstring to name the new caught type and its non-audit rationale.

   Ordering: the new `except` is a sibling of the `except RoleDenied`. `ProjectMembershipDenied`
   (subclass of `AuthError`) and `RoleDenied` (subclass of `AuthorizationError`) are unrelated, so
   either order is correct; place the membership clause after `RoleDenied`.

### Step 3 — Handler-level behavior is unchanged (raise), pinned

1. **Test:** confirm/adjust the existing direct-handler foreign-project tests stay raise-asserting:
   - `tests/mcp/accounting/test_accounting_usage.py:test_usage_foreign_project_refused`
   - `tests/mcp/accounting/test_accounting_admin_tools.py:test_set_budget_foreign_project_refused`

   They currently catch `AuthError`; `ProjectMembershipDenied` **is** an `AuthError`, so they pass
   unchanged. Tighten the caught type to `ProjectMembershipDenied` in **one** of them to pin the
   new type at the handler layer. Do **not** convert them to expect an envelope.
2. The existing `require_project` property test
   (`tests/adversarial/test_auth_properties.py:test_require_project_matches_context_membership`)
   and `tests/mcp/core/test_auth.py:test_require_project_validates_membership` keep
   `pytest.raises(AuthError)` — still valid. Optionally tighten one to `ProjectMembershipDenied`.

### Step 4 — CLI exit-3 via the enveloped-response path

1. **Test (red):** in `tests/cli/test_tool_error_handling.py`, add an `_EnvelopingClient` whose
   `call_tool` **returns** a structured `authorization_denied` envelope (the real server-denial
   shape, mirroring a `ToolResponse.failure(..., AUTHORIZATION_DENIED)` serialization the CLI read
   path consumes). Drive the same `allocations list` verb and assert the CLI exit code is **3** via
   `exit_code_for_envelope`. Keep the existing `_RaisingClient` test asserting **exit 1** (the
   backstop for non-denial `ToolError`s) and update its module docstring to note the split.
2. **Code:** none expected — `cli/errors.py`/`cli/dispatch.py` are unchanged by design. Confirm the
   curated read verb path (`cli/commands/reads.py` → `exit_code_for_envelope`) already returns 3 for
   the enveloped category.

### Step 5 — No-leak regression pin

1. **Test:** in the allocations tool tests, assert an **ungranted by-id** `get_allocation`
   (an allocation whose `project` is not in `ctx.projects`) returns `error_category == "not_found"`
   and **never** `"authorization_denied"`. (If an equivalent assertion already exists from #338,
   extend it with the explicit `!= authorization_denied` clause so this change is pinned.)

### Step 6 — Docs reconciliation

1. `docs/runbooks/kdivectl.md`: the project-axis read row and the "Two project-axis outcomes"
   prose currently imply a non-member naming a project gets exit 1 (or is described only for the
   by-id not-found case). Reconcile to state: a **non-member who names a project** in a
   named-scope read/op (`allocations list`, `accounting.usage_project`, `accounting.estimate`,
   …) now gets `authorization_denied` (**exit 3**) — distinct from the **by-id** `get`/`show`
   not-found-shaped result (exit 4), which is unchanged. Keep the no-leak wording for the by-id
   case intact. Cite ADR-0098.
2. Regenerate any generated docs invalidated by the change (tool-docs etc.) via the
   `just docs-check`/`config-docs-check` workflow; commit regenerated artifacts.

## Guardrails (before every commit; CI runs recipes individually)

```
just lint && just type && just test
just docs-check config-docs-check check-mermaid
```

Zero warnings. `ty` is the type gate (ignore editor Pyright noise).

## Commits (Conventional Commits, one logical change each)

1. `docs(adr): add ADR-0098 membership-denial envelope + spec + plan` (the docs already staged)
2. `feat(authz): raise ProjectMembershipDenied from require_project`
3. `fix(mcp): envelope membership denials as authorization_denied at dispatch boundary`
4. `test(cli): assert enveloped membership denial exits 3; keep raised-error exit-1 backstop`
5. `docs(runbook): reconcile named-scope denial exit-3 vs by-id not-found`

(Group sensibly; keep each green under guardrails. Each message ends with the required
`Co-Authored-By` trailer.)

## Risks / failure modes

- **Catching too broadly.** Must catch the *subclass* `ProjectMembershipDenied`, never bare
  `AuthError` — else a missing-token authentication failure (`current_context`) would wrongly
  envelope as exit-3. Pinned by the "authentication still raises" test (spec Test #6).
- **Audit amplification.** The membership clause must **not** write an audit row. Pinned by the
  zero-rows assertion in Step 2.
- **No-leak bleed.** By-id getters never call `require_project`; pinned by Step 5.
