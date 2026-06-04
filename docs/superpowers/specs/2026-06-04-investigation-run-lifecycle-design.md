# Investigation + Run lifecycle & tools — Design

**Issue:** #17 (M0) · **Depends on:** #7 (repository layer / locks / idempotency —
merged), #10 (FastMCP skeleton / auth — merged), #16 (provisioning plane / System
lifecycle — merged) · **Decisions:**
[ADR-0026](../../adr/0026-investigation-run-lifecycle.md) (the decisions this spec
realizes), [ADR-0003](../../adr/0003-six-durable-objects.md) (object model + binding
invariant), [ADR-0016](../../adr/0016-repository-layer-locks-idempotency.md) (advisory
locks), [ADR-0019](../../adr/0019-tool-response-envelope.md) (response envelope),
[ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md) (RBAC/audit),
[ADR-0023](../../adr/0023-discovery-allocation-admission.md) (tool-surface conventions),
[ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) (System lifecycle) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Domain objects in M0 → Investigation/Run", "MCP tool surface → Investigate/Run",
"Failure & retry", exit-criteria spine steps `investigations.open` and `runs.create`)

## Goal

The Investigation campaign and the Run join-point of the walking skeleton, wired onto the
existing domain model:

- `src/kdive/mcp/tools/investigations.py` — `investigations.open` / `.get` / `.close` /
  `.link` / `.unlink`: the project-scoped campaign that groups Runs and carries mutable
  `external_refs` to external trackers.
- `src/kdive/mcp/tools/runs.py` — `runs.create` / `.get`: the Run join-point. `create`
  binds a Run to a `ready` System (fixing its Allocation, [ADR-0003](../../adr/0003-six-durable-objects.md))
  and an Investigation, and flips the Investigation `open → active` on its first Run.

Plus the minimal plumbing the above require:

- `src/kdive/db/locks.py` — add `LockScope.INVESTIGATION` (ADR-0026 §3).
- `src/kdive/mcp/app.py` — append `investigations.register` and `runs.register` to
  `_PLANE_REGISTRARS`. Neither registers a job handler (no new `JobKind`; both surfaces are
  synchronous).

This layer sits **above** the repository/locks/RBAC/audit primitives and the System
lifecycle, and **below** the agent. It owns *the Investigation campaign surface*, *the Run
join-point and its binding-invariant enforcement*, and *the first-Run activation of an
Investigation*. It does **not** own the build/install/boot steps that drive a Run
`created → running → …` (a later plane), nor the reconciler's `abandoned` sweep.

## Non-goals

- **No `runs.build` / `.install` / `.boot`.** #17 creates a Run in `created` and reads it;
  the job-dispatched steps that drive `created → running → succeeded`/`failed` and write
  `run_steps` are a later build-plane issue. `runs.get` *renders* `running`/`succeeded`/
  `failed`/`canceled` for forward-compatibility, but no #17 tool produces them. Stated so
  the Run staying in `created` is not read as incomplete.
- **No `BuildProfile` model / validation.** `build_profile` is stored as opaque `jsonb`
  and validated only for "is a JSON object"; the typed model lands with the build plane
  that owns it (parallel to `provisioning_profile` before
  [ADR-0024](../../adr/0024-provisioning-profile-model-shape.md)). `runs.create` exposes
  no profile-schema parameter and performs no field checks.
- **No Run cancellation / failure tooling.** Driving a Run to `canceled` (`jobs.cancel`)
  or `failed` (step failure / lease expiry) is the build-plane / reconciler's job; #17
  adds neither `runs.cancel` nor a failure path.
- **`runs.create` is not idempotent in M0 (deliberate).** Like `allocations.request`
  (ADR-0023; that spec's non-goal), `runs.create` is synchronous and carries no
  `dedup_key` or client idempotency token, so a client retry after a timeout inserts a
  **second** Run on the same `(investigation_id, system_id)`. This is intentional: an
  Investigation legitimately groups *many* Runs on one System (each `runs.build`/`install`/
  `boot` cycle is a fresh Run — the spec's retry-as-a-new-Run recovery model), so there is
  no natural uniqueness rule to enforce, and a duplicate Run is **inert until a step is
  invoked** — it sits in `created`, consuming no compute, until the agent issues
  `runs.build` (a later plane), at which point the agent's own sequencing makes a stray
  duplicate visible. The blast radius is therefore a recoverable extra `created` Run, not a
  double build. A client-supplied idempotency token is the proper fix and is deferred to
  the build-plane work that owns the long-running step admission (where `dedup_key`
  `(run_id, step, kind)` already lives). Stated here so non-idempotent `runs.create`
  retries are a recorded decision, not an oversight.
- **No reconciler.** The `last_run_at` column #17 maintains is the *input* the M1
  idle-Investigation sweep will consume; #17 writes it but runs no sweep, and the
  `abandoned` state is unreachable by any #17 tool.
- **No `external_refs` schema beyond `{tracker, id, url}`.** The three string fields of
  `ExternalRef` are the whole contract; no tracker-specific validation (URL well-formedness,
  known tracker names) is performed.
- **No cross-Investigation or cross-System listing.** #17 ships no `investigations.list`
  or `runs.list` (not in the issue's tool set); `get` is by id only. A list surface, if
  needed, is a later addition.
- **No new gated/`live_vm` test.** Nothing here touches libvirt/gdb/drgn; every behavior is
  covered against a real Postgres with hand-built contexts and seeded rows.

## Components

### `investigations.py` — the Investigation campaign surface

```python
async def open_investigation(pool, ctx, *, project, title, external_refs=None) -> ToolResponse: ...
async def get_investigation(pool, ctx, investigation_id) -> ToolResponse: ...
async def close_investigation(pool, ctx, investigation_id) -> ToolResponse: ...
async def link_external_ref(pool, ctx, investigation_id, ref) -> ToolResponse: ...
async def unlink_external_ref(pool, ctx, investigation_id, ref) -> ToolResponse: ...
def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...
```

Every handler takes `pool` + `ctx` as arguments (tested directly, never through MCP) and
wraps its body in `bind_context(principal=ctx.principal)` (ADR-0014) so records emitted
while serving are attributed. `_as_uuid` (malformed → `configuration_error`) and a
`_config_error` helper mirror `allocations.py` / `systems.py`.

- **`investigations.open(project, title, external_refs?)`**
  1. `require_project(ctx, project)` then `require_role(ctx, project, Role.OPERATOR)`.
  2. Parse `external_refs` (a list of `{tracker, id, url}` dicts) into `list[ExternalRef]`;
     a malformed entry → `failure(project, CONFIGURATION_ERROR)` **before** any write.
     Deduplicate the parsed list by the `(tracker, id)` natural key (last-wins), so an
     `open` carrying two refs for the same tracked item lands one entry — the same identity
     rule `link` enforces.
  3. `INVESTIGATIONS.insert` an Investigation: `state=OPEN`, `title`, `external_refs`,
     attribution from `ctx`, `last_run_at=None`. Audit `transition="->open"`,
     `object_kind="investigations"`, `args={"project": project, "title": title}` (the
     digest is one-way; the title is low-sensitivity but is not stored in plaintext on the
     audit row regardless).
  4. Return `success(str(inv.id), "open", suggested_next_actions=["investigations.get",
     "runs.create"], data={"project": project})`.
- **`investigations.get(investigation_id)`** — parse the uuid, `INVESTIGATIONS.get`; a
  `None` row or one whose `project` is not in `ctx.projects` → `_config_error`
  (not-found-shaped, no cross-project leak). Otherwise `_envelope_for_investigation(inv)`.
- **`_envelope_for_investigation(inv)` (shared by `get` and the mutators' final read).** No
  Investigation state collides with the envelope failure-status set
  (`open`/`active`/`closed`/`abandoned` ∉ `{"failed", "error"}`), so **every** state renders
  as `success(str(inv.id), inv.state.value, suggested_next_actions=[…],
  data={"project": inv.project, "external_refs": str(len(inv.external_refs))})`. Suggested
  actions vary by state: non-terminal → `["investigations.get", "investigations.close",
  "runs.create"]`; terminal → `["investigations.get"]`.
- **`investigations.close(investigation_id)`** — read the row + ownership check + `require_role(OPERATOR)`,
  then under `advisory_xact_lock(INVESTIGATION, id)` re-read the state and:
  - `open` / `active` → `update_state(CLOSED)` + audit `"<old>->closed"`; return the closed
    envelope.
  - already `closed` → idempotent `success(..., "closed")` (no transition, no audit).
  - `abandoned` → `failure(CONFIGURATION_ERROR, data={"current_status": "abandoned"})` (a
    distinct terminal; not closable). An `IllegalTransition` backstop (caught outside the
    rolled-back transaction, re-read) maps any escaping illegal edge to the same
    `configuration_error`, mirroring `allocations.release`.
- **`investigations.link(investigation_id, ref)`** — `ref` is a `{tracker, id, url}` dict.
  Parse it to an `ExternalRef` (malformed → `configuration_error`); read the row + ownership
  + `require_role(OPERATOR)`; then under `advisory_xact_lock(INVESTIGATION, id)`:
  - re-read the row **`FOR UPDATE`**; a terminal (`closed`/`abandoned`) Investigation →
    `failure(CONFIGURATION_ERROR, data={"current_status": …})` (immutable).
  - else upsert: drop any existing ref whose `(tracker, id)` equals the new ref's, append
    the new ref, `UPDATE investigations SET external_refs = %s` (the new list, wrapped in
    `Jsonb(...)` — psycopg3 does not adapt a bare `list` to `jsonb`). Audit
    `transition="link"`, `args={"tracker": ref.tracker, "id": ref.id}`.
  - return `_envelope_for_investigation(updated)`.
- **`investigations.unlink(investigation_id, ref)`** — `ref` is keyed on the **`(tracker,
  id)`** natural key only: unlink accepts `{tracker, id}` (a `url` is **not** required, and
  any supplied `url` is ignored — matching never consults it), so a caller that holds only
  the tracked-item identity can unlink without fabricating a `url`. Parse `ref` for a
  non-empty string `tracker` and `id` (a missing/empty either → `configuration_error`);
  **do not** require the full `ExternalRef`. Same guard/lock shape as `link`; remove the ref
  whose `(tracker, id)` matches (absent match → idempotent: write the unchanged list / skip
  the write, still `success`). Audit `transition="unlink"` only when a ref was actually
  removed. Return the updated envelope.

The read-modify-write of `external_refs` **must** hold the per-Investigation lock across the
re-read and the `UPDATE`, or two concurrent `link`s each read the old list and the second's
write clobbers the first's appended ref (lost update). The `FOR UPDATE` row read under the
advisory lock is belt-and-suspenders against any non-advisory writer.

### `runs.py` — the Run join-point

```python
async def create_run(pool, ctx, *, investigation_id, system_id, build_profile) -> ToolResponse: ...
async def get_run(pool, ctx, run_id) -> ToolResponse: ...
def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...

_RUN_HOSTABLE = frozenset({SystemState.READY})
_SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
_ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
_INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})
```

- **`runs.create(investigation_id, system_id, build_profile)`**
  1. Parse both uuids (malformed → `configuration_error`). Require `build_profile` is a
     non-empty `dict` (an empty `{}` is allowed — the build plane owns content validation;
     "is a JSON object" is the only check). A non-dict → `configuration_error`.
  2. Read the Investigation; `None` or `project ∉ ctx.projects` → `_config_error`
     (not-found-shaped). `require_role(ctx, inv.project, Role.OPERATOR)`.
  3. Read the System; `None` or `project ∉ ctx.projects` → `_config_error`. A System whose
     `project != inv.project` → `failure(CONFIGURATION_ERROR)` (cross-project join is not a
     valid Run). The Run's `project` is `inv.project`.
  4. **Binding invariant (lock-free read).** Read the System's Allocation; if its state is
     **not** in `_ALLOC_HOSTABLE` (i.e. not `active`) → `failure(STALE_HANDLE,
     data={"current_status": <alloc state>})`. This is an **allowlist**, symmetric with the
     System check (`_RUN_HOSTABLE`): a `ready` System should only ever sit under an `active`
     Allocation (provisioning flips `granted → active`), so the binding requires `active`
     rather than merely "not terminal". The allowlist also rejects `releasing` and any
     future non-`active`-but-non-terminal state (M1 leasing) without a denylist that rots
     silently — the System's Allocation is gone or going; the binding is broken (ADR-0026 §2).
  5. Open one transaction; acquire `advisory_xact_lock(SYSTEM, system_id)` **then**
     `advisory_xact_lock(INVESTIGATION, investigation_id)` (the global order). Re-read the
     System state under the lock:
     - state ∈ `_SYSTEM_GONE` → `failure(STALE_HANDLE, data={"current_status": <state>})`.
     - state ∉ `_RUN_HOSTABLE` (i.e. `defined`/`provisioning`) →
       `failure(CONFIGURATION_ERROR, data={"current_status": <state>})`.
     - state == `ready` → proceed.
  6. Re-read the Investigation state under the lock (`FOR UPDATE`); admit **only** when the
     state ∈ `_INVESTIGATION_OPEN_FOR_RUN` (i.e. `open`/`active`), else
     `failure(CONFIGURATION_ERROR, data={"current_status": …})` (cannot add a Run to a
     `closed`/`abandoned` campaign). This is an **allowlist**, uniform with the System and
     Allocation checks: a future non-terminal Investigation state is rejected-by-default
     until the gate is revisited, rather than silently admitted by a terminal-only denylist.
  7. `RUNS.insert` a Run: `state=CREATED`, `investigation_id`, `system_id`, `build_profile`,
     attribution = `inv.project` + `ctx.principal`/`ctx.agent_session`, `failure_category=None`.
     Audit `transition="->created"`, `object_kind="runs"`,
     `args={"investigation_id": …, "system_id": …}`.
  8. If the Investigation is `open` → `update_state(ACTIVE)` + audit
     `"open->active"` (`object_kind="investigations"`). Always
     `UPDATE investigations SET last_run_at = now() WHERE id = …`. The flip and the Run
     insert share the transaction, so they commit together; the per-Investigation lock makes
     the flip exactly-once under concurrent first-Runs. **Transaction-nesting hazard
     (`systems.py` precedent):** `audit.record` opens **no** transaction of its own, and
     `INVESTIGATIONS.update_state` opens a nested savepoint; the `open->active` audit
     `INSERT` must run inside the **outer** `runs.create` transaction (the one holding the
     locks and the Run insert), or — on a non-autocommit pool connection — a bare audit
     `INSERT` would be rolled back when the connection is returned. The plan/code keep the
     `update_state` and its audit row in that one outer transaction.
  9. Return `success(str(run.id), "created", suggested_next_actions=["runs.get",
     "runs.build"], data={"project": inv.project, "investigation_id": …, "system_id": …})`.
     (`runs.build` is named as the literal next action even though it ships later — the
     `suggested_next_actions` are the *intended* spine, and the agent learns the sequence.)
- **`runs.get(run_id)`** — parse the uuid, `RUNS.get`; `None` or `project ∉ ctx.projects` →
  `_config_error`. Otherwise `_envelope_for_run(run)`.
- **`_envelope_for_run(run)`.** The Run state value `"failed"` collides with the envelope
  failure-status set, so a `failed` Run →
  `failure(str(run.id), run.failure_category or INFRASTRUCTURE_FAILURE,
  data={"current_status": "failed"})` (the model's `failure_category` is the precise
  category the build/boot plane recorded; `INFRASTRUCTURE_FAILURE` is the documented default
  when it is unexpectedly `NULL`). `canceled` is **not** a failure status →
  `success(..., "canceled")`. `created`/`running`/`succeeded` → `success(..., state)`.
  Suggested actions: `created`/`running` → `["runs.get", "runs.build"]`; terminal →
  `["runs.get"]`.

`runs.create` holds the System lock across the state re-read and the Run insert, so a
concurrent `systems.teardown` either commits `torn_down` first (and the re-read rejects with
`stale_handle`) or waits behind the lock (and tears down a System that now has a Run — the
reconciler's orphaned-Run concern, not a corruption). The Investigation lock, taken second,
serializes the activation flip.

### `locks.py` / `app.py` changes

- **`locks.py`** adds `LockScope.INVESTIGATION = "investigation"`; the key derivation
  (`blake2b(scope ‖ \x00 ‖ uuid)`) is unchanged and already accepts any scope. The global
  acquisition order `ALLOCATION → SYSTEM → INVESTIGATION → RUN` is documented in the module
  docstring (no `RUN` scope is added — #17 needs none; it is reserved in the ordering note).
- **`app.py`** appends `investigations.register` and `runs.register` to `_PLANE_REGISTRARS`;
  `_HANDLER_REGISTRARS` is unchanged (no new job kind).

## Threat model & guarantees

- **A Run never binds to a dead or orphaned System (at commit).** The System lock is held
  across the `ready` re-read and the Run insert, so a teardown cannot drive the System
  terminal between the check and the insert; the lock-free Allocation check additionally
  rejects an already-released Allocation. The only residual is a release that commits
  between the Allocation read and the insert — a transient state the reconciler repairs.
- **The `open → active` flip is exactly-once and atomic with the Run.** The per-Investigation
  lock serializes concurrent first-Runs (one flip, one audit row); the shared transaction
  makes "Run exists ⇒ Investigation is `active`" hold after commit. A crash mid-create rolls
  back both.
- **`external_refs` edits do not lose updates.** Every `link`/`unlink` read-modify-writes
  under the per-Investigation lock; concurrent edits serialize. `link`/`unlink` are
  idempotent on the `(tracker, id)` key, so a client retry neither duplicates nor errors.
- **No cross-project leak.** `get` on an unowned Investigation/Run, and `create` naming an
  unowned Investigation/System, return the same `configuration_error` as a missing row.
- **Mutations are operator-gated; reads are membership-gated.** Authorization denials raise
  (no authz `ErrorCategory`, ADR-0020), exactly as #14 established.
- **Audit attribution rides on `audit.record`'s own `project ∈ ctx.projects` guard**; every
  transition (`->open`, `->created`, `open->active`, `<old>->closed`, `link`, `unlink`)
  writes exactly one row inside the same transaction as the change.

## Failure modes & edges (drives the tests)

All tests use the real-Postgres fixtures (`migrated_url`, the `asyncio.run(_run())` idiom),
seed Investigations/Systems via the repositories, and hand-build `RequestContext` with
explicit `roles` — mirroring `test_systems_tools.py` / `test_allocations_tools.py`.

**investigations.open**
- operator, valid → `success` `status="open"`, real `investigation_id`; exactly one
  `audit_log` row `transition="->open"`; the row persists `state="open"`, the title, and the
  parsed `external_refs`.
- with `external_refs=[{tracker,id,url}, …]` → the refs round-trip on the row; two refs
  sharing `(tracker, id)` collapse to one (last-wins dedup).
- malformed `external_refs` entry (missing `url`, non-str field) → `failure(CONFIGURATION_ERROR)`,
  **no** row written.
- viewer / no role → `AuthorizationError`; `project` not granted → `AuthError`. No row.

**investigations.get**
- own-project → `success` with the state and `data.external_refs` count.
- cross-project id / missing id / malformed uuid → `failure(CONFIGURATION_ERROR)`
  (indistinguishable).

**investigations.close**
- `open` → `success` `"closed"`, one `"open->closed"` audit row, row ends `closed`.
- `active` → `success` `"closed"` via `active->closed`.
- already `closed` → idempotent `success` `"closed"`, **no** new audit row.
- `abandoned` → `failure(CONFIGURATION_ERROR, data.current_status="abandoned")`, row
  unchanged.
- viewer → `AuthorizationError`.
- concurrent `close` of one `open` Investigation (two coroutines/connections) → exactly one
  `"open->closed"` audit row; no `IllegalTransition` escapes (the backstop is also unit-tested
  by forcing the catch path).

**investigations.link / unlink**
- `link` a fresh ref → `success`; the ref is on the row; one `"link"` audit row.
- `link` an existing `(tracker, id)` with a changed `url` → the `url` is updated in place
  (still one entry for that key — upsert), not duplicated.
- `link` the identical ref twice → one entry (idempotent).
- `unlink` an existing ref → removed from the row; one `"unlink"` audit row.
- `unlink` with `{tracker, id}` **and no `url`** → removes the matching ref (the input
  contract needs only the natural key); `unlink` with a `url` that differs from the stored
  ref's still removes it (matching ignores `url`).
- `unlink` an absent `(tracker, id)` → idempotent `success`, list unchanged, **no** audit row.
- `link`/`unlink` on a `closed`/`abandoned` Investigation → `failure(CONFIGURATION_ERROR,
  data.current_status=…)`, row unchanged.
- malformed `ref` dict → `failure(CONFIGURATION_ERROR)` before any read/lock.
- **acceptance spine:** `open → link → unlink` leaves `external_refs` empty again, with the
  intermediate `link` state observed (the issue's first acceptance criterion).
- concurrent `link` of two **different** refs on one Investigation → both refs present after
  (no lost update — proves the per-Investigation lock serializes the read-modify-write; a
  bare run without the lock would drop one).

**runs.create**
- operator, `ready` System (active Allocation), `open` Investigation → `success`
  `status="created"`, real `run_id`; the Investigation flips `open->active` (one such audit
  row) and `last_run_at` is set; the Run row has `state="created"`, the `build_profile`, and
  `project=inv.project`.
- second `runs.create` on the now-`active` Investigation → `success`; **no** second
  `open->active` audit row; `last_run_at` advances.
- `torn_down` System → `failure(STALE_HANDLE, data.current_status="torn_down")` (the issue's
  third acceptance criterion); `failed`/`crashed` System → likewise `stale_handle`.
- `defined`/`provisioning` System → `failure(CONFIGURATION_ERROR, data.current_status=…)`.
- System whose Allocation is **not `active`** (System still `ready`) →
  `failure(STALE_HANDLE, data.current_status=<alloc state>)` (binding invariant, allowlist;
  ADR-0026 §2). Cover at least a `released` Allocation (the live, reachable case); a
  `requested`/`granted` Allocation under a `ready` System is non-reachable in M0 but the
  allowlist rejects it too — assert the `active` Allocation admits and a non-`active` one
  does not.
- `closed`/`abandoned` Investigation → `failure(CONFIGURATION_ERROR, data.current_status=…)`,
  no Run, no flip.
- System and Investigation in **different** projects → `failure(CONFIGURATION_ERROR)`.
- missing/cross-project Investigation or System id, malformed uuid → `failure(CONFIGURATION_ERROR)`.
- non-dict / missing `build_profile` → `failure(CONFIGURATION_ERROR)`, no Run.
- viewer → `AuthorizationError`, no Run, no flip.
- concurrent first-`runs.create` on one `open` Investigation (two coroutines, two `ready`
  Systems) → both Runs created, exactly **one** `open->active` audit row (proves the
  per-Investigation lock makes the flip exactly-once). The two Systems must be distinct so
  the System locks do not serialize the test into sequentiality — the Investigation lock is
  the one under test.
- teardown race: a `systems.teardown` committing `torn_down` while `runs.create` waits on the
  System lock → `runs.create` re-reads `torn_down` and returns `stale_handle` (the lock makes
  the rejection deterministic, not flaky).

**runs.get**
- own-project `created`/`running`/`succeeded` → `success` with the state.
- `failed` Run with `failure_category="build_failure"` → `failure(error_category="build_failure",
  data.current_status="failed")`; a `failed` Run with `failure_category=NULL` →
  `failure(error_category="infrastructure_failure")` (documented default).
- `canceled` Run → `success` `"canceled"` (not a failure status).
- cross-project / missing / malformed uuid → `failure(CONFIGURATION_ERROR)`.

**locks / app**
- `LockScope.INVESTIGATION` derives a distinct lock key from `SYSTEM`/`ALLOCATION`/`RESOURCE`
  for the same uuid (no cross-scope collision) — extend `tests/db/test_locks.py`.
- `build_app` registers `investigations.*` and `runs.*` (the tool names are present on the
  app) — extend `tests/mcp/test_app.py`.

## Testing strategy

Handlers are the unit of testing (repo contract): call `open_investigation` /
`get_investigation` / `close_investigation` / `link_external_ref` / `unlink_external_ref`
and `create_run` / `get_run` **directly** with injected `pool` + hand-built `ctx` — never
through MCP. No provider, libvirt, or `live_vm` is involved (Investigations/Runs are pure
Postgres objects); Systems are seeded `ready` via `SYSTEMS.insert` and a `granted`/`active`
Allocation via `ALLOCATIONS.insert`, reusing the `test_systems_tools.py` seeding helpers'
shape (a `_granted_allocation` → `systems.provision`-free direct seed, or an `_active`
Allocation for the binding test).

- Tests live in `tests/mcp/test_investigations_tools.py` and `tests/mcp/test_runs_tools.py`,
  mirroring the package layout; `tests/db/test_locks.py` and `tests/mcp/test_app.py` gain the
  lock-scope and registration assertions.
- Concurrency tests (the `open->active` exactly-once flip, the `external_refs` lost-update
  guard, the close race) use two connections from the pool and force the lock-contended
  interleaving deterministically — a bare `asyncio.gather` that lets the first commit before
  the second issues its lock proves nothing (it passes against a no-op lock), so the
  Investigation-lock tests assert against a *held* lock or a barrier, per the
  `test_allocation_admission.py` serialization-proof pattern.
- No new gated/`live_vm` test; nothing here needs libvirt/gdb/drgn at run time.
