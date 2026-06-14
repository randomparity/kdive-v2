# Spec — Envelope project-membership denials as `authorization_denied` (issue #339, finding S2)

- **Date:** 2026-06-12
- **ADR:** [0098](../../adr/0098-membership-denial-envelope.md)
- **Issue:** #339
- **Status:** Draft

## Problem

The same condition — *"not authorized for this project"* — surfaces two ways depending on the
tool, with two different CLI exit codes:

- The **`require_project` membership gate** (`src/kdive/security/authz/context.py`) raises
  `AuthError` → `fastmcp.ToolError` → the CLI dispatch catch (`src/kdive/cli/dispatch.py`, #337)
  collapses it to the **generic exit 1**, no `error_category`.
- The **platform-role gates** and the **`require_role` member-over-reach gate** return an
  `authorization_denied` envelope → **exit 3** (`src/kdive/cli/errors.py`).

A script/CI cannot reliably branch on "authorization denied": half the surface is exit 3 + typed
category, half is generic exit 1 + opaque string. ADR-0020 §4 ("authz denials raise, no
category") is now the minority path; `AUTHORIZATION_DENIED` exists and the platform/role gates
already envelope.

## Goal

Every authorization denial across the MCP/CLI surface maps to **exit 3** with
`error_category="authorization_denied"`, uniformly machine-readable — **without** disturbing the
ADR-0097 by-id `not_found` no-leak path.

| Cause | category | exit | site |
|-------|----------|------|------|
| Caller names a project they are not a member of | `authorization_denied` | 3 | `require_project` (after this change) |
| Member whose role ranks below the floor | `authorization_denied` | 3 | `require_role` → `RoleDenied` (already) |
| Platform-role gate denied | `authorization_denied` | 3 | `require_platform_role` (already) |
| **By-id** object in an ungranted/absent project | `not_found` | 4 | by-id getters' `not in ctx.projects` pre-emption (**unchanged**, ADR-0097) |
| Authentication failure (no subject / no token) | raises (hard error) | 1 | `current_context`/`context_from_claims` (**unchanged**) |

## Invariants (must hold)

1. **No-leak (load-bearing, ADR-0097/ADR-0020).** A by-id lookup that resolves to a row in an
   *ungranted* project MUST still return `not_found` — **never** `authorization_denied`.
   Distinguishing "ungranted, exists" from "absent" is a cross-tenant existence oracle. This spec
   touches **only** the `require_project` named-scope gate; the by-id getters do **not** call
   `require_project` (they use the `obj.project not in ctx.projects → not_found` pattern), so they
   are structurally outside the change. Pinned by a regression test.

2. **Authentication still raises.** `AuthError` from a missing/invalid token
   (`current_context`, `context_from_claims`) is **not** a project-authorization denial and must
   stay a raised hard error — it must **not** become an `authorization_denied` envelope. The new
   exception is a **subclass** (`ProjectMembershipDenied`) the dispatch boundary catches
   *specifically*; the bare `AuthError` base is never caught there.

3. **Audit trail unchanged.** Member-over-reach (`RoleDenied`) is still audited; the non-member
   membership denial is still **not** audited (ADR-0043 §4 write-amplification protection). No new
   `audit_log` row shape, no migration.

4. **Stable categories untouched.** `authorization_denied` keeps its wire string and existing
   producers; no new category is added.

## Design

### 1. `ProjectMembershipDenied` exception (`src/kdive/security/authz/errors.py`)

```python
class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request."""


class ProjectMembershipDenied(AuthError):
    """The caller named a project they are not a member of (ADR-0098).

    Subclasses AuthError so existing membership semantics/catches are preserved; the
    dispatch boundary catches this *subclass* specifically to envelope it as
    authorization_denied (exit 3), while a bare AuthError (authentication failure) keeps
    raising.
    """
```

### 2. `require_project` raises the subclass (`src/kdive/security/authz/context.py`)

```python
def require_project(ctx: RequestContext, project: str) -> str:
    if project not in ctx.projects:
        raise ProjectMembershipDenied(
            f"project {project!r} is not granted to {ctx.principal!r}"
        )
    return project
```

No call-site changes at the 8 `require_project` callers.

### 3. `DenialAuditMiddleware` envelopes the membership denial
(`src/kdive/mcp/middleware.py`)

The middleware already catches `RoleDenied` and returns
`ToolResponse.failure(tool, AUTHORIZATION_DENIED)`. Add a sibling `except` for
`ProjectMembershipDenied` that returns the **same** envelope **without** auditing (the
non-member case stays unaudited). Catch the membership case **before** any base-class handling so
ordering is explicit; `ProjectMembershipDenied` and `RoleDenied` are unrelated types so the order
between them is irrelevant, but the membership branch must not fall through to a `RoleDenied`
audit.

```python
try:
    return await call_next(context)
except RoleDenied as denial:
    ...  # audit + envelope (unchanged)
except ProjectMembershipDenied:
    # Non-member denial: enveloped, NOT audited (ADR-0043 §4 — no write-amplification).
    return ToolResponse.failure(context.message.name, ErrorCategory.AUTHORIZATION_DENIED)
```

### 4. CLI: no code change

`cli/errors.py:exit_code_for_envelope` already maps `authorization_denied → 3`. The
`dispatch.py` `ToolError → 1` catch stays as the backstop for *other* raised tool errors;
membership denials no longer reach it.

## Call sites changed

| File | Change |
|------|--------|
| `src/kdive/security/authz/errors.py` | add `ProjectMembershipDenied(AuthError)` |
| `src/kdive/security/authz/context.py` | `require_project` raises `ProjectMembershipDenied` |
| `src/kdive/mcp/middleware.py` | `DenialAuditMiddleware` catches `ProjectMembershipDenied`, envelopes (no audit) |
| `src/kdive/mcp/auth.py` | re-export `ProjectMembershipDenied` if it re-exports `AuthError` (keep import surface consistent) |
| `docs/runbooks/kdivectl.md` | reconcile the named-project read row: a non-member naming a project now gets exit 3, not exit 1 |

**Not changed (by design):** the 8 `require_project` callers; every by-id getter
(`allocations.get/release/renew`, `investigations.get`, vmcore-run targets, runs/systems views);
`require_role`/`RoleDenied`; the destructive-op gate; `cli/dispatch.py`; `cli/errors.py`.

## Tests

**The envelope is produced at the `DenialAuditMiddleware` seam, not in the tool handler.**
`require_project` raises `ProjectMembershipDenied` *inside* the handler; the handler therefore
still **raises**. Only when the call passes through `DenialAuditMiddleware.on_call_tool` is the
raised exception turned into an `authorization_denied` envelope. Tests must assert each behavior
**at its own layer** — a bare-handler test asserts the raise; a middleware-harness test asserts
the envelope. Conflating the two produces a test that cannot pass.

New / updated:

1. **Membership denial envelopes — at the middleware (unit).** Drive
   `DenialAuditMiddleware.on_call_tool` over a `call_next` that raises `ProjectMembershipDenied`
   (mirroring the existing `test_role_denied_*` / `test_base_authorization_error_is_not_audited`
   cases in `tests/mcp/core/test_denial_audit_middleware.py`, which already provide the
   `_FakeContext`/`call_next` harness). Assert the returned `ToolResponse.error_category ==
   "authorization_denied"` **and** that **zero** `audit_log` rows are written (the membership denial
   is the non-member case — deliberately **unaudited**, ADR-0043 §4). This is the *new* surfacing
   behavior.

2. **`require_project` raises `ProjectMembershipDenied` — at the handler (unit).** A direct call to
   `require_project(ctx, ungranted)` raises `ProjectMembershipDenied`; and a direct handler call
   (`usage_project`/`set_budget`/`estimate`/`list_allocations`) for an ungranted project still
   **raises** (it does not return an envelope — the handler has no middleware in the path).

3. **Existing direct-handler foreign-project tests REMAIN raise-asserting.**
   `tests/mcp/accounting/test_accounting_usage.py:test_usage_foreign_project_refused` and
   `tests/mcp/accounting/test_accounting_admin_tools.py:test_set_budget_foreign_project_refused`
   call the handlers directly (no middleware), so they keep asserting a **raise**. The handler
   contract is unchanged because `ProjectMembershipDenied` **is** an `AuthError`. Do **not** churn
   them to expect an envelope; at most tighten the caught type from `AuthError` to
   `ProjectMembershipDenied`.

4. **CLI exit 3 (was exit 1) — via the enveloped-response path.** In
   `tests/cli/test_tool_error_handling.py`, add a client that **returns** an
   `authorization_denied` envelope (the real server-denial shape) and assert the CLI derives
   **exit 3** through `exit_code_for_envelope`. **Keep** the existing `_RaisingClient` case (a
   raised `ToolError`) asserting **exit 1** — that is the backstop for *non-denial* tool errors,
   which the change deliberately preserves. Do not repurpose the raising mock to assert exit 3.

5. **No-leak pinned (regression).** An ungranted **by-id** `allocations.get` still returns
   `not_found` (exit 4), **never** `authorization_denied` — the by-id getters never call
   `require_project`, so the membership-envelope change cannot bleed into them. Pin explicitly on
   `allocations.get` (which #338 moved to `not_found`): assert the category is `not_found`, never
   `authorization_denied`. For `investigations.get` assert only the load-bearing property — the
   category is **not** `authorization_denied` (it remains `configuration_error` today; its
   `not_found` migration is #338's scope, not this change's — do **not** migrate it here).

6. **Authentication still raises.** A missing-token `current_context` / bad-subject
   `context_from_claims` still raises a bare `AuthError` (not `ProjectMembershipDenied`, not
   enveloped) — the middleware catch is on the *subclass*, so authentication failures are untouched.

7. **Existing membership unit/property tests adapt.** `tests/mcp/core/test_auth.py` and
   `tests/adversarial/test_auth_properties.py` assert `pytest.raises(AuthError)` on
   `require_project` — still valid since `ProjectMembershipDenied` **is** an `AuthError`; optionally
   tighten one to `pytest.raises(ProjectMembershipDenied)`.

## Out of scope

- Auditing the non-member denial (deliberately excluded — ADR-0043 §4).
- Any change to the by-id `not_found` no-leak path (ADR-0097).
- A new error category or exit code (reuse `authorization_denied` / exit 3).
