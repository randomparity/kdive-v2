# ADR 0098 — Envelope project-membership denials as `authorization_denied`

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-12
- **Deciders:** kdive maintainers
- **Supersedes (in part):** [ADR-0020](0020-rbac-audit-gate-implementation.md) §4 — "authorization
  failures raise" — for the read/named-scope tool surface (see Scope).
- **Refines:** [ADR-0043](0043-platform-scoped-rbac-tier.md) §4/§4a (platform vs. project authorization
  axes), [ADR-0089](0089-operator-cli-mcp-client.md) (CLI exit-code contract).
- **Relates:** [ADR-0097](0097-not-found-conflict-error-categories.md) (the by-id no-leak
  `not_found` path this ADR must **not** disturb).

## Context

The same user-facing condition — *"you are not authorized for this project"* — surfaces two
different ways depending on which tool the caller hits, and the two map to **different CLI exit
codes** and have different machine-readability (finding **S2**, issue #339):

- **Raise → generic exit 1.** A tool that names a project and gates membership with
  `require_project` (`src/kdive/security/authz/context.py`) lets the resulting `AuthError`
  propagate as a `fastmcp.ToolError`. The CLI dispatch boundary
  (`src/kdive/cli/dispatch.py`, added in #337) catches it, prints a one-line stderr message, and
  returns the **generic** exit code `1`. There is no typed `error_category`.
  Producers: `allocations.list`, `allocations.request`, `accounting.usage_project`,
  `accounting.usage_investigation`, `accounting.estimate`, `accounting.set_budget`,
  `accounting.set_quota`, `investigations.open` — every site that calls `require_project`.

- **Envelope → exit 3.** The platform-role gates
  (`require_platform_role`, e.g. `inventory.list`, `secrets.list`,
  `ops.tuning`, `catalog.shapes`, the `platform_admin` gate in `cli/commands/mutations.py`) and
  the project-role **member-over-reach** gate (`require_role` → `RoleDenied`, enveloped at the
  dispatch boundary by `DenialAuditMiddleware`) return a
  `ToolResponse.failure(..., AUTHORIZATION_DENIED)` envelope, which `cli/errors.py` maps to the
  stable exit code **`3`**.

A script or CI cannot reliably branch on "authorization denied": half the surface emits a typed
exit 3 with an `error_category`, the other half emits a generic exit 1 and an opaque string.

ADR-0020 §4 decided M0 authz failures *raise* and carry **no** `ErrorCategory`, explicitly
deferring the wire mapping to "the first handler that must return a denial as a response"
(ADR-0020 Consequences). That handler has since arrived: `AUTHORIZATION_DENIED` was added to the
taxonomy (`src/kdive/domain/errors.py`) and the platform gates and `require_role` member gate
already envelope. Only the `require_project` **membership** gate still raises. The codebase
straddles both conventions; ADR-0020's "raise" decision is now the minority path.

## Decision

**Project-membership denials envelope as `authorization_denied` (exit 3), uniformly.**

1. **`require_project` raises a typed `ProjectMembershipDenied`** (a subclass of `AuthError`, in
   `src/kdive/security/authz/errors.py`) instead of a bare `AuthError`. The message and
   "membership" semantic are unchanged; only the type narrows so the dispatch boundary can
   discriminate it. Because it subclasses `AuthError`, existing call sites and the
   "membership is validated" tests keep working; the narrowing is additive.

2. **`DenialAuditMiddleware` catches `ProjectMembershipDenied` at the dispatch boundary** and
   returns the **same** `ToolResponse.failure(tool, AUTHORIZATION_DENIED)` envelope it already
   returns for `RoleDenied`. This is the single tool-dispatch seam; the 8 `require_project` call
   sites are **not** individually modified (one boundary, not eight `try/except` blocks).

3. **The membership denial is NOT audited** — preserving the *exact* current behavior. The
   middleware audits **only** member-over-reach (`RoleDenied`); the non-member case has always
   been deliberately excluded (ADR-0043 §4 / ADR-0062 §5) to prevent write-amplification: any
   authenticated token could otherwise spam `audit_log` denial rows on an openly-callable read by
   naming projects it is not in. A `require_project` denial **is** the non-member case, so it
   inherits that exclusion. The audit trail is therefore *unchanged*: member-over-reach denials
   are still audited; non-member denials are still not.

4. **The CLI side needs no code change.** After (2) the denial flows out of the tool as a normal
   `authorization_denied` envelope; `cli/errors.py:exit_code_for_envelope` already maps it to
   exit `3`. The `dispatch.py` `ToolError → exit 1` catch (#337) remains as the backstop for any
   *other* raised `ToolError` (e.g. a genuine server fault), but membership denials no longer
   reach it.

### Scope of the ADR-0020 supersession

ADR-0020 §4 is superseded **only** for the surfacing of project-membership and project-role
denials as tool responses on the read/named-scope surface. Specifically:

- `AuthError` keeps raising for genuine **authentication** failures (no usable subject, no token
  in context — `current_context`/`context_from_claims`): those are not project-authorization
  denials and must stay hard errors, not `authorization_denied` envelopes.
- The destructive-op gate (`assert_destructive_allowed` → `DestructiveOpDenied`) and its
  denied-op audit composition (ADR-0020 §3, Consequences) are **unchanged**.
- `AuthorizationError`/`RoleDenied` semantics are unchanged; this ADR only adds the *membership*
  exception alongside them.

## The two conditions are distinct and surface differently — on purpose

This ADR envelopes a **membership/role gate on a caller-named scope**. It must not be confused
with — and explicitly does **not** touch — the **by-id object-existence** no-leak path of
[ADR-0097](0097-not-found-conflict-error-categories.md):

| Condition | Site | Surfacing | Why |
|-----------|------|-----------|-----|
| Caller **names a project** and is not a member | `require_project(ctx, project)` (named in call args) | `authorization_denied` (exit 3) | The project name is already in the request; revealing "you're not authorized for the project you named" leaks nothing the caller didn't supply. Matches the platform-role and member-over-reach gates, which already surface exit 3 for a named scope. |
| Caller passes an **object id** that resolves to an ungranted (or absent) row | by-id getters resolve the row, then `if obj.project not in ctx.projects: return not_found` **before** any role check (`allocations.get/release/renew`, `investigations.get`, vmcore-run targets, runs/systems views) | `not_found` (exit 4), **identical** to a genuinely absent id | Distinguishing "ungranted, exists" from "absent" would be a **cross-tenant existence oracle**: a caller could probe whether another tenant's object exists. The two must be indistinguishable. |

The discriminator is **what the caller supplied**: a *project name they chose* (no existence to
leak) versus an *opaque id* whose mere existence in another tenant must stay hidden. The by-id
getters reach `require_role` **only after** the `not_found` pre-emption has already accepted
membership, so they never raise the membership denial and are untouched by this ADR. The no-leak
invariant of ADR-0097 is preserved verbatim.

## Consequences

- Every authorization denial across the MCP/CLI surface now maps to **exit 3** with a typed
  `authorization_denied` `error_category`. A script/CI can branch on it uniformly.
- The change is concentrated at one seam (`require_project` raises a narrower type; the existing
  denial middleware catches one more exception type). No tool handler gains a `try/except`.
- The audit trail is byte-for-byte unchanged: the non-member denial remains unaudited (no
  write-amplification regression); member-over-reach remains audited.
- The by-id no-leak `not_found` paths (ADR-0097) are untouched and remain pinned by their tests; a
  regression test asserts an ungranted by-id lookup still returns `not_found` (never
  `authorization_denied`).
- `ProjectMembershipDenied` subclasses `AuthError`, so any future code that catches `AuthError`
  broadly still catches it; only the dispatch boundary discriminates the subclass. No migration or
  DDL is needed (no new `audit_log` row shape; the denial is intentionally unaudited).

## Alternatives considered

- **Catch `AuthError` in each of the 8 tools and envelope locally.** Rejected: it scatters the
  same `try/except` across eight handlers (and every future `require_project` caller would have to
  remember it), and `AuthError` is also raised by `current_context` for a missing token — a local
  catch risks turning a genuine 401-ish defense-in-depth failure into a tool-level
  `authorization_denied`. The single-seam middleware catch on a *distinct subclass* avoids both.

- **Map the raised `AuthError` to exit 3 in `cli/dispatch.py`** (the issue's stated alternative).
  Rejected: it fixes only the *CLI* exit code, not the *MCP wire response* — an agent calling the
  tool directly (not via the CLI) would still get a raised `ToolError` with no `error_category`,
  so the surface stays non-uniform off the CLI. Enveloping at the server seam makes the denial
  machine-readable for **every** client, and the CLI exit code falls out for free.

- **Make the membership denial a distinct category / audit it.** Rejected on both counts: a
  distinct category (vs. reusing `authorization_denied`) would re-introduce a second
  authorization wire string for no benefit; auditing the non-member denial would reverse the
  deliberate ADR-0043 §4 write-amplification protection.

- **Move `require_project`'s membership check into the by-id getters' `not_found` pattern (drop
  the named-project gate).** Rejected: the named-project tools (`*.list`, `usage_project`,
  `estimate`) have **no object id** to resolve — they operate on a project name the caller
  supplies, so there is no existence to hide and `not_found` would be a category error. The two
  conditions are genuinely different (see the table above).
