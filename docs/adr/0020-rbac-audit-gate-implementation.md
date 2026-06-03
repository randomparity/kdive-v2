# ADR 0020 — RBAC roles, audit record, and the destructive-op gate (M0 shapes)

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-03
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0006](0006-oidc-rbac-attribution.md) (OIDC/RBAC + attribution)

## Context

[ADR-0006](0006-oidc-rbac-attribution.md) fixes the *policy*: three project-scoped
roles (`viewer`/`operator`/`admin`) asserted by the IdP, and a destructive op gated
by three independent, all-required checks (allocation capability scope, `admin`
role, explicit profile/flag opt-in). It leaves the *implementation shapes* open, and
[the MCP-skeleton design](../superpowers/specs/2026-06-03-mcp-skeleton-auth-jobs-design.md)
explicitly defers them to issue #11: the role claim's name/shape, where roles live
on the request context, how `record()` participates in a transition's transaction,
and the interface of `assert_destructive_allowed`. This ADR pins those shapes so the
plane-tool issues (#13+) that *call* the primitives compile against a stable surface.
It governs `src/kdive/security/{rbac,audit,gate}.py` for issue #11; see
[the m0 spec](../specs/m0-walking-skeleton.md) "Auth, RBAC & attribution" and
"Cross-cutting in M0 → Audit".

## Decision

We will ship the three primitives with these shapes:

1. **Roles ride on `RequestContext`.** We add `roles: Mapping[str, Role]` (project →
   the principal's single highest role on that project) to the existing
   `RequestContext` (`src/kdive/mcp/auth.py`), parsed from a `roles` token claim
   (`{project: role-string}`) by `rbac.roles_from_claims`. The role *vocabulary and
   policy* (`Role` enum, the `viewer < operator < admin` rank, `require_role`,
   parsing) live in `rbac.py`; `auth.py` only calls the parser to populate the field.
   To avoid a runtime import cycle (`auth → rbac` at runtime; `rbac → RequestContext`
   for types only), `rbac.py` imports `RequestContext` under `TYPE_CHECKING` and is
   duck-typed at runtime; the one genuine runtime edge — `roles_from_claims` raising
   `auth.AuthError` on a malformed claim — uses a function-level import so `rbac`'s
   module-level dependency on `auth` stays type-only. `require_role(ctx, project, role)` denies unless `project`
   is in `ctx.projects` (membership) **and** the principal's role on it ranks ≥ the
   required role; a member carrying no role on the project is a denial, guarded before
   any rank lookup so it never raises `KeyError`. The `roles` field is declared
   `compare=False` so the still-`frozen` `RequestContext` stays hashable (a `dict`
   field would otherwise make `__hash__` raise `TypeError`); roles are a derived view of
   the same token as the attribution tuple, so excluding them from equality changes no
   real outcome.

2. **`record` writes exactly one append-only row inside the caller's transaction.**
   `audit.record(conn, ctx, *, tool, object_kind, object_id, transition, args,
   project)` issues one `INSERT` into `audit_log` and returns the new row id. It does
   **not** open its own transaction, so a caller wraps the state transition and the
   `record` call in one `conn.transaction()` and they commit (or roll back)
   atomically — the foundation for "every state transition writes exactly one audit
   row". `args_digest` is the SHA-256 hex of a canonical JSON encoding of `args`
   (`sort_keys=True`, compact separators, `default=str`); the raw `args` are never
   stored. The digest is tamper-evidence/correlation, **not** confidentiality of
   low-entropy values (those are brute-forceable from the log); secret confidentiality
   is ADR-0012's job — callers pass secret references, not raw values, in `args`.
   `project` is a required argument because `ctx.projects` is the *granted set*, not the
   single project the audited object belongs to; `record` asserts `project in
   ctx.projects` and raises `AuthError` otherwise, a last-line integrity guard on the
   attribution the append-only row carries forever.

3. **The gate's third factor rides on `op`.** `assert_destructive_allowed(ctx,
   allocation, op)` composes three independent checks and raises
   `DestructiveOpDenied` listing **every** missing check (not just the first):
   - (a) capability scope — `op.kind` is listed under `allocation.capability_scope`'s
     `destructive_ops` key;
   - (b) admin role — `require_role(ctx, allocation.project, Role.ADMIN)` passes;
   - (c) profile opt-in — `op.profile_opt_in` is `True`.
   `op` is a frozen `DestructiveOp(kind: str, profile_opt_in: bool = False)`; the
   handler resolves the explicit opt-in from the System's provisioning profile/flag
   and passes it. `profile_opt_in` defaults to `False` (deny-by-default).

4. **Authorization failures raise, with a two-way split.** `AuthError` (already in
   `auth.py`) stays "who are you / are you a member of this project" (no subject,
   project not granted). RBAC/gate denials raise a new `AuthorizationError` ("you may
   not do this"); `DestructiveOpDenied` subclasses it and carries the missing checks.
   Neither carries an `ErrorCategory`: the M0 taxonomy
   ([ADR-0001](0001-greenfield-rewrite.md)) has no authorization category and "do not
   invent strings" forbids adding one here; mapping a denial onto a wire category is
   the calling handler's concern and is deferred to the plane-tool issues.

## Consequences

- The plane tools (#13+) get a stable surface: `require_role(ctx, project, role)`,
  `record(conn, ctx, …, project=…)` inside their transition transaction, and
  `assert_destructive_allowed(ctx, allocation, op)` at the top of every destructive
  handler. The three checks are individually testable by flipping one input.
- `security` now depends on `kdive.mcp.auth` for the `RequestContext` type. This is
  the transport-composes-policy direction and is acceptable for M0; if a non-MCP
  surface ever needs the context, `RequestContext` moves to a neutral module in that
  issue, not this one.
- The `roles` claim name/shape and the `capability_scope.destructive_ops` key are
  **provisional contracts** pinned here. The allocation issue that owns
  `capability_scope`'s typed interior, and #13/IdP integration that owns the real
  claim set, must honor these keys or update this ADR.
- The gate stays a pure policy check with no `conn` and never writes audit rows;
  **denied** destructive attempts are audited by the handler that catches
  `DestructiveOpDenied` (`transition=f"{op.kind}:denied"`, `args` carrying
  `missing`), the same composition as a granted transition. `DestructiveOpDenied.missing`
  exists to feed that record. Recorded so a refused destructive op leaves an audit
  trail rather than vanishing; the wiring is a later-handler-issue obligation.
- A denial has no wire `ErrorCategory` yet. The first handler that must return a
  denial as a `ToolResponse.failure` will force a taxonomy decision (a new
  `authorization_denied` category is the likely outcome) — recorded here as a known
  follow-on, not solved in #11.
- ADR-0006 allows `operator` to perform a destructive op "only where the op's profile
  opt-in permits". M0 does **not** implement that relaxation: the gate requires
  `admin` unconditionally (matching the m0 spec's "(b) `admin` role"). The
  operator-exception is deferred (see Alternatives).

## Alternatives considered

- **Derive project membership from the roles claim (drop the separate `projects`
  claim).** Rejected for M0: `projects` is already shipped and tested by #10 as the
  membership grant, and `require_project` is its consumer. Folding the two risks a
  silent behavior change to merged code; keeping `roles` additive and having
  `require_role` re-check membership closes the consistency gap (a role on a
  non-granted project is simply unusable) without touching #10's contract.

- **Store a `set` of roles per project rather than the single highest role.**
  Rejected: the three roles are a total rank, so a set is always representable as its
  maximum; a single role with rank comparison is simpler and matches "M0's operator
  holds `admin` for the project". A future non-hierarchical capability set is a
  separate claim, not a widening of this one.

- **Pass the System/provisioning-profile to the gate so it reads the opt-in itself.**
  Rejected: it would couple the gate to the provisioning-profile schema (owned by
  [ADR-0011](0011-provisioning-profile-schema.md) / a later issue) and widen the
  fixed `(ctx, allocation, op)` signature's meaning. Carrying the resolved boolean on
  `op` keeps the gate pure policy over three inputs and keeps profile-schema knowledge
  in the handler.

- **`record` opens its own transaction / runs after the transition commits.**
  Rejected: a separate transaction breaks atomicity — a crash between the transition
  commit and the audit insert yields a transition with no audit row (or the reverse),
  violating "exactly one row per transition". `record` must enlist in the caller's
  transaction.

- **Implement the ADR-0006 `operator`-with-opt-in relaxation now.** Rejected for M0:
  the m0 spec's destructive-gate acceptance names `admin` as factor (b), and the
  three-tests acceptance is cleanest with one role threshold. The relaxation is a
  policy widening best added with a real operator-tier use case and its own tests.

- **Add an `authorization_denied` `ErrorCategory` in #11.** Rejected: no M0 tool
  surfaces a denial as a response yet (the destructive handlers are later issues), so
  the category would be a phantom value until then ("no phantom features"). It lands
  with its first real producer.
