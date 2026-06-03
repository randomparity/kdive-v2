# RBAC, audit log & destructive-op gate — Design

**Issue:** #11 (M0) · **Depends on:** #7 (repository layer / `audit_log` table —
merged), #10 (MCP skeleton / `RequestContext` — merged) · **Decisions:**
[ADR-0006](../../adr/0006-oidc-rbac-attribution.md) (OIDC/RBAC + attribution),
[ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md) (the M0 implementation
shapes this spec realizes), [ADR-0019](../../adr/0019-tool-response-envelope.md)
(response envelope) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md) ("Auth,
RBAC & attribution", "Cross-cutting in M0 → Audit", exit criterion 6)

## Goal

The three security primitives every later plane tool composes:

- `src/kdive/security/rbac.py` — the `Role` enum (`viewer`/`operator`/`admin`) with a
  total rank, `roles_from_claims` (parse a `roles` token claim), and
  `require_role(ctx, project, role)`.
- `src/kdive/security/audit.py` — `record(...)`: one append-only `audit_log` row per
  call, inside the caller's transaction, with a one-way `args_digest`.
- `src/kdive/security/gate.py` — `assert_destructive_allowed(ctx, allocation, op)`:
  the three-check destructive gate (capability scope, `admin` role, profile opt-in).

Plus the minimal plumbing to thread roles through the request context:
`RequestContext` (`src/kdive/mcp/auth.py`) gains a `roles: Mapping[str, Role]` field,
and `context_from_claims` populates it via `rbac.roles_from_claims`.

This layer sits **above** the repository/auth layers (#7, #10) and **below** the
plane handlers (#13+) that call it. It owns *who may do what* and *what gets
audited*; it does **not** own *when* a transition happens (the handler) or the
wire-level response mapping of a denial (the handler, see Non-goals).

## Non-goals

- **No repository or worker wiring.** Per the issue's Files list and the scoping
  decision on #11, `record`/`require_role`/the gate are **not** called from
  `db/repositories.py` or `jobs/worker.py` in this issue. The
  "every transition writes exactly one audit row" property is delivered as the
  *contract* of `record` (transactional, append-only, one row) and **proven** by a
  test that performs a real `StatefulRepository.update_state` and a `record` in one
  transaction and asserts exactly one `audit_log` row. The per-handler wiring is owned
  by the plane-tool issues (#13+) that introduce the transitions. (Stated so the
  unused-at-the-repository-layer primitives are not mistaken for dead code.)
- **No `ErrorCategory` for denials.** `require_role`/the gate **raise**
  (`AuthorizationError`/`DestructiveOpDenied`); they do not build a `ToolResponse`.
  The M0 taxonomy has no authorization category and "do not invent strings" forbids
  adding one with no producer ([ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md)).
  The first destructive handler (a later issue) maps the denial onto a response.
- **No IdP / claim-issuance work.** This consumes a `roles` claim from an
  already-verified token (#10 owns verification). The claim *name/shape* is pinned
  here as the provisional contract #13/IdP integration must honor (ADR-0020).
- **No redaction port.** `args_digest` is a hash, so secrets in `args` are never
  *revealed* by the audit row regardless of redaction. The redactor
  (`security/redaction.py`, #23) is a separate concern for guest-output bytes, not the
  audit digest.
- **No `operator`-with-opt-in relaxation.** ADR-0006 allows `operator` to perform a
  destructive op where the profile opt-in permits; M0 requires `admin`
  unconditionally (m0 spec factor (b)). Deferred (ADR-0020 Alternatives).
- **No capability-scope typed model.** The gate reads one documented key
  (`destructive_ops`) from the `dict[str, Any]` `capability_scope`; the typed interior
  lands with the allocation issue that owns it.

## Components

### `rbac.py` — roles & enforcement

```python
class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

def roles_from_claims(claims: Mapping[str, object]) -> dict[str, Role]: ...
def require_role(ctx: RequestContext, project: str, role: Role) -> None: ...
```

- **`Role`** — the three M0 roles as stable wire strings (they match the claim's role
  values and the spec vocabulary). The `_RANK` map encodes the total order
  `viewer < operator < admin`; a higher role satisfies a lower requirement.
- **`roles_from_claims(claims)`** reads the `roles` claim, a JSON object mapping a
  project name to a single role string (`{"proj-a": "admin", "proj-b": "operator"}`).
  - Missing/`None` `roles` claim → `{}` (a token may grant membership via `projects`
    without any role; such a principal is effectively `viewer`-less and fails every
    `require_role`).
  - The claim must be a `dict`; a non-object (`list`, `str`, …) raises `AuthError`
    (malformed token, consistent with `context_from_claims`'s other claim checks).
  - Each value must be a known `Role` string; an unknown role (`"superadmin"`) raises
    `AuthError` (fail closed — never silently drop an unrecognized grant to nothing,
    and never treat it as a known role). Keys are coerced to `str`.
  - Returns a plain `dict[str, Role]`.
- **`require_role(ctx, project, role)`** raises `AuthorizationError` unless **both**:
  (1) `project in ctx.projects` — membership; and (2) the principal holds at least the
  required role on that project. The algorithm is exactly:

  ```python
  if project not in ctx.projects:
      raise AuthorizationError(...)          # not a member
  held = ctx.roles.get(project)              # Role | None
  if held is None or _RANK[held] < _RANK[role]:
      raise AuthorizationError(...)          # no role, or role too low
  ```

  `held is None` (member granted via `projects` but carrying no role on it) and
  `_RANK[held] < _RANK[role]` (role too low) are **both** denials — the `None` case is
  guarded *before* any `_RANK` lookup, so a member-without-a-role yields a clean
  `AuthorizationError`, never a `KeyError` escaping the security layer. Returns `None`
  on success. Re-checking membership makes a stray `roles` entry on a non-granted
  project unusable without trusting claim consistency. The message names the principal,
  project, required vs. held role (no secret material).

### `audit.py` — the append-only record

```python
async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    object_kind: str,
    object_id: UUID,
    transition: str,
    args: Mapping[str, object],
    project: str,
) -> UUID: ...

def args_digest(args: Mapping[str, object]) -> str: ...   # sha256 hex
```

- **`args_digest(args)`** — `hashlib.sha256` of a canonical JSON encoding:
  `json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)`, UTF-8
  encoded, hex digest. **Input contract:** `args` are JSON-native (they originate from
  an MCP/JSON tool call — objects, arrays, strings, numbers, booleans, null), plus the
  two scalar non-native types the codebase routinely carries, `UUID` and `datetime`,
  which `default=str` renders deterministically. `default=str` is a scalar safety net
  **only**: an *unordered or identity-dependent* container (a `set`, or an object whose
  `__str__` is not stable) has no canonical `str` and would make the digest
  non-deterministic, breaking the "same args → same digest" correlation property. Such
  a value is a caller bug, not a supported input; the determinism claim holds for the
  declared JSON-native-plus-`UUID`/`datetime` domain. `sort_keys=True` canonicalizes
  object key order (top-level and nested). The digest is one-way, so no plaintext from
  `args` is stored — satisfying "args_digest never contains secret material" literally.
  See "Threat model & guarantees" for what this does and does **not** protect (it is
  tamper-evidence/correlation, not confidentiality of low-entropy values).
- **`record(...)`** issues exactly one
  `INSERT INTO audit_log (principal, agent_session, project, tool, object_kind,
  object_id, transition, args_digest) VALUES (…) RETURNING id`. `id`/`ts` are
  DB-generated (defaults). `principal`/`agent_session` come from `ctx`; `project` is
  the explicit argument (the audited object's project), **not** `ctx.projects` (the
  granted *set*). Returns the new row's `id`.
- **`project` is cross-checked, not trusted.** The audit log is the append-only
  security-of-record; a wrong `project` writes a permanently un-rewritable,
  misattributed row and silently breaks per-project audit queries. `record` therefore
  asserts `project in ctx.projects` and raises `AuthError` otherwise — cheap, and it
  catches the common handler bug (auditing object A under project B, or passing a
  project the principal was never granted) before the row is written. This is the
  audited object's *own* project (the one the handler authorized via
  `require_project`/`require_role`); it must be a member of `ctx.projects` by
  construction, so the assertion only ever fires on a caller bug. It is **not** a
  substitute for the handler having authorized the operation — it is a last-line
  integrity guard on the attribution the row will carry forever.
- **Transactionality.** `record` runs its `INSERT` on the passed `conn` and does
  **not** open a transaction. The caller composes the state transition and `record`
  inside one `conn.transaction()` so both commit atomically (or neither does). This is
  how "exactly one audit row per transition" holds under a mid-operation crash.
- **Append-only.** `record` only `INSERT`s; the module exposes no update/delete, and
  `audit_log` has no `updated_at`/trigger (schema `0001_init.sql`). Append-only is
  structural, not enforced by a runtime guard.

### `gate.py` — the three-check destructive gate

```python
@dataclass(frozen=True)
class DestructiveOp:
    kind: str               # "force_crash" | "power" | "teardown" | …
    profile_opt_in: bool = False

_DESTRUCTIVE_OPS_KEY = "destructive_ops"

def assert_destructive_allowed(
    ctx: RequestContext, allocation: Allocation, op: DestructiveOp
) -> None: ...
```

`assert_destructive_allowed` evaluates **all three** checks and raises
`DestructiveOpDenied(missing=[…])` if any failed (listing every missing check, so an
audit/log line shows the full reason, not just the first failure):

| # | check | passes when | data source |
|---|-------|-------------|-------------|
| a | capability scope | `op.kind in allocation.capability_scope.get("destructive_ops", ())` | `allocation.capability_scope` (jsonb) |
| b | admin role | `require_role(ctx, allocation.project, Role.ADMIN)` does not raise | `ctx.roles` |
| c | profile opt-in | `op.profile_opt_in is True` | `op` (handler-resolved) |

- Check (b) calls `require_role` and converts its `AuthorizationError` into the
  `"admin_role"` missing-check entry, so the gate raises one uniform
  `DestructiveOpDenied` rather than two exception types.
- All three present → returns `None`. `op.profile_opt_in` defaults to `False`, so a
  handler that forgets to resolve and pass the opt-in is denied (deny-by-default).
- `capability_scope.get` tolerates a non-dict/absent scope as "no destructive ops
  granted" → check (a) fails closed.
- **Residual risk on factor (c) — a handler can collapse the gate to two checks.**
  Factors (a) and (b) are data-driven (the gate reads `allocation.capability_scope` and
  `ctx.roles` itself); factor (c) is a boolean the *handler* constructs, so a handler
  that hardcodes `profile_opt_in=True` defeats it and the gate cannot tell. ADR-0020
  deliberately keeps the gate from reading the provisioning-profile schema, so this
  residual trust in the handler is accepted — but it is **contained**, not ignored: (1)
  each destructive handler's tests **must** assert `profile_opt_in` is resolved from the
  System's provisioning profile, not a constant (a contract on those later issues,
  recorded here so it is not forgotten); and (2) the destructive op's `record(...)` call
  audits `args` that include the opt-in's source key, so a hardcoded bypass is visible
  in the audit trail after the fact. The gate's job is to compose three inputs
  fail-closed; proving factor (c)'s input is real is the handler's job and its test's.

**Denied attempts are audited, not just refused.** A refused destructive op is a
security event — an attacker probing the gate, or a misconfigured agent, must leave a
trail in the append-only log, not vanish. `record` audits *transitions*, but the audit
schema (`object_kind`, `transition`, `args_digest`) is general enough to record an
*attempt* that produced no transition, so the contract is: **the handler that catches
`DestructiveOpDenied` calls `record` before re-raising/returning the denial**, with
`object_kind` = the target object's kind, `transition = f"{op.kind}:denied"`, and `args`
including the refusal reason (`{"missing_checks": exc.missing, …}`). `DestructiveOpDenied`
carries `missing` precisely so the handler has the reason to audit without
re-deriving it. This is a handler obligation (the destructive handlers are later
issues, so #11 owns no caller) recorded here so denial-auditing is a tracked decision,
not an accidental blind spot; a granted op audits its real `transition` after the
transition commits, a denied op audits `f"{op.kind}:denied"` with no transition. The
gate primitive itself does **not** call `record` — it has no `conn`, and coupling a
pure policy check to a DB write would make every gate call a transaction; auditing is
the handler's composition, same as for granted transitions.

### `auth.py` — context plumbing (minimal change)

```python
@dataclass(frozen=True)
class RequestContext:
    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict, compare=False)
```

`context_from_claims` sets `roles=roles_from_claims(claims)`. The default keeps every
existing direct construction of `RequestContext` (tests, handlers) valid with an empty
role map. To avoid a runtime import cycle, `rbac.py` imports `RequestContext` only
under `TYPE_CHECKING`; `auth.py` imports `Role`/`roles_from_claims` from `rbac` at
runtime, and its `roles: Mapping[str, Role]` annotation is a string at runtime
(`from __future__ import annotations`, already present) so `Role` is needed only for
typing. The **one** genuine runtime edge `rbac → auth` is `roles_from_claims` raising
`auth.AuthError` on a malformed claim: it imports `AuthError` with a **function-level**
import inside `roles_from_claims` (off the happy path), so `rbac`'s module-level
dependency on `auth` stays type-only and the cycle is broken. (Function-level imports
are already used this way in `src/kdive/__main__.py` and are lint-clean — ruff enables
no rule against them.)

**Hashability.** `RequestContext` is `frozen=True`, so its autogenerated `__hash__`
hashes every comparable field. A `dict` field is unhashable, which would silently turn
the previously-hashable context into one that raises `TypeError` the first time it is
put in a set / used as a dict key / passed to `lru_cache`. The new field therefore
uses `compare=False`, excluding `roles` from **both** `__eq__` and `__hash__`:
`RequestContext` stays hashable over `(principal, agent_session, projects)`. This loses
nothing real — roles are a *derived authorization view* of the same verified token that
produced the attribution tuple, so two contexts with equal `(principal, agent_session,
projects)` necessarily carry equal `roles` (both parsed from one token). A test asserts
`hash(ctx)` does not raise and that `roles` is still readable, so a future change back
to a hashable-but-compared mapping is a deliberate choice, not an accident.

### Errors

```python
class AuthorizationError(Exception): ...                 # rbac.py
class DestructiveOpDenied(AuthorizationError):           # gate.py
    def __init__(self, missing: list[str]) -> None: ...
    missing: list[str]
```

`AuthError` (auth.py, unchanged) = authentication-adjacent / membership failures.
`AuthorizationError` = RBAC denial. `DestructiveOpDenied` = gate denial, carrying the
missing checks (a subset of `{"capability_scope", "admin_role", "profile_opt_in"}`).
Neither authz error carries an `ErrorCategory` (ADR-0020).

## Threat model & guarantees

What each primitive does and does **not** promise, so callers do not over-trust it:

- **`args_digest` — tamper-evidence and correlation, not confidentiality.** The SHA-256
  guarantees no plaintext is stored and lets two audit rows be compared for "same
  args", and detects after-the-fact tampering if the raw args were re-derivable.
  It does **not** keep a *low-entropy* value secret: anyone with read access to
  `audit_log` can brute-force a short token, PIN, enum, or boolean by hashing the
  candidate space and matching the digest. Confidentiality of secret material is **not**
  the digest's job — it is owned by ADR-0012 (secrets-by-reference) and the redactor
  (#23). The contract on callers is therefore: per ADR-0012, `args` carry secret
  *references*, not raw secret values; the digest then commits to a reference, and the
  brute-force concern does not arise. The digest is a defence-in-depth backstop for the
  case a caller violates that contract, not the primary control.
- **`record` — integrity of attribution rests on one cheap guard plus caller
  discipline.** `record` asserts `project in ctx.projects` (above) so a misattributed
  project is caught, but it does **not** verify that `object_id`/`object_kind`/
  `transition` describe a real, just-performed transition — a handler that calls
  `record` without performing the transition (or vice versa) is not detected here. The
  "exactly one row per transition" property holds only when the handler wraps the
  transition and the `record` call in one transaction (the contract above); `record`
  cannot enforce that its caller did so. Correct pairing is the handler's
  responsibility and is covered by the handler-level issues.
- **The gate — fail-closed composition, with factor (c) trusting the handler.** See the
  gate's "Residual risk" note: (a) and (b) are gate-verified against data; (c) trusts a
  handler-supplied boolean, contained by a handler-test contract and the audit trail.
- **`object_kind` / `transition` vocabulary is not enforced.** Both are free `text` in
  the schema and free `str` here. Inconsistent values across handlers (`"system"` vs
  `"systems"`) would fragment audit queries. M0 does not pin the vocabulary (no handlers
  exist yet to standardize); the convention is recorded for the plane issues:
  `object_kind` = the durable object's table name, `transition` = `f"{old}->{new}"`.

## Failure modes & edges (drives the tests)

**rbac**
- `roles_from_claims`: absent claim → `{}`; `{"p":"admin"}` → `{"p": Role.ADMIN}`;
  non-dict claim → `AuthError`; unknown role value → `AuthError`; non-str keys coerced.
- `require_role`: held > required (admin for operator-required) → ok; held == required
  → ok; held < required (operator for admin) → `AuthorizationError`; project not in
  `ctx.projects` → `AuthorizationError`; project granted but **absent from `roles`**
  (`held is None`) → `AuthorizationError`, **not** `KeyError` (the regression guard for
  finding 1).
- `RequestContext` hashability: a context with a non-empty `roles` map → `hash(ctx)`
  does not raise, and `roles` is still readable (the regression guard for the
  `frozen`+`dict` hazard).

**audit**
- `args_digest`: deterministic across key reorder (incl. a nested object); differs for
  differing args; a known secret string in `args` does **not** appear in the digest
  (hex, secret not a substring); `UUID`/`datetime` scalar values encode without error
  and reproduce the same digest twice (locks the `default=str` scalar safety-net domain
  from finding 2).
- `record`: one call → exactly one `audit_log` row with the expected
  principal/agent_session/project/tool/object_kind/object_id/transition and a digest
  matching `args_digest(args)`; `agent_session=None` persists as SQL `NULL`; returns
  the row id.
- `record` project guard: a `project` **not** in `ctx.projects` → `AuthError` and
  **no** row written (the integrity guard for finding 4).
- transition+audit atomicity: a real `update_state` plus `record` in one transaction →
  exactly one audit row (the "per transition" property); rolling back the transaction
  leaves **zero** audit rows (proves enlistment in the caller's transaction).
- append-only: the module exposes no update/delete entry point (asserted structurally
  in the test/by inspection).

**gate**
- all three present → returns `None` (allowed).
- scope absent (op.kind not in `destructive_ops`) → `DestructiveOpDenied(["capability_scope"])`.
- not admin (operator role) → `DestructiveOpDenied(["admin_role"])`.
- opt-in false (default) → `DestructiveOpDenied(["profile_opt_in"])`.
- multiple absent → `missing` lists all of them.
- denial-audit shape (finding 1): `DestructiveOpDenied.missing` is populated and ordered
  so a catching handler can audit `transition=f"{op.kind}:denied"`,
  `args={"missing_checks": missing}` — asserted on the exception, since #11 owns no
  handler caller (the wiring is a later-issue contract).
- `capability_scope` missing the key / not a dict → scope check fails closed.

## Testing strategy

Primitives are the unit of testing (repo contract): call `roles_from_claims`,
`require_role`, `args_digest`, `record`, `assert_destructive_allowed` directly with
hand-built `RequestContext`/`Allocation`/`DestructiveOp` values. `rbac` and `gate` are
pure and need **no** DB. `audit.record` and the transition-atomicity test use the
existing testcontainers Postgres fixtures (`migrated_url`, the async idiom
`asyncio.run(_run())`) from `tests/db/conftest.py` — promoted to a shared location (or
imported) so `tests/security/` can reuse them. The three destructive-gate acceptance
tests flip exactly one factor each. No new gated integration tests; nothing here needs
libvirt/gdb/drgn.

Tests live in `tests/security/` mirroring the package: `test_rbac.py`,
`test_audit.py`, `test_gate.py`.
