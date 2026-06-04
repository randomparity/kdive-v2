# ADR 0026 — Investigation + Run lifecycle & tools (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #17 (M0: Investigation + Run lifecycle & tools)
- **Depends on:** [ADR-0016](0016-repository-layer-locks-idempotency.md) (repository
  layer / advisory locks), [ADR-0010](0010-fastmcp-framework-auth.md) (FastMCP / auth),
  [ADR-0019](0019-tool-response-envelope.md) (response envelope),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (RBAC / audit),
  [ADR-0023](0023-discovery-allocation-admission.md) (the tool-surface conventions this
  follows), [ADR-0025](0025-provisioning-plane-libvirt.md) (System lifecycle this binds to),
  [ADR-0003](0003-six-durable-objects.md) (the object model and the binding invariant)
- **Refines:** the M0 Investigation/Run wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) ("Domain objects
  in M0 → Investigation/Run", "MCP tool surface → Investigate/Run", "Failure & retry")

## Context

Issue #17 wires the **Investigation** campaign and the **Run** join-point onto the
existing domain model. The durable models (`Investigation`, `Run`, `ExternalRef`), their
state machines, the `INVESTIGATIONS` / `RUNS` repositories, and the Postgres tables all
already exist (shipped by #7); #17 adds only the two tool modules
(`investigations.*`, `runs.*`) and the small plumbing they require.

`Run` is the join of the provisioning chain (`Resource → Allocation → System`) and the
Investigation campaign — the only tool surface that touches both hierarchies. Six
decisions are either unpinned by the parent spec or pinned loosely, so they are settled
here.

## Decision

### 1. `runs.create` admits only a `ready` System; dead states are `stale_handle`, pre-ready states are `configuration_error`

A Run drives `build → install → boot` on a live, provisioned System; the walking
skeleton creates a Run only after provisioning reaches `ready`. `runs.create` therefore
admits **only** a System in state `ready` and maps every other state to a typed failure:

| System state | `runs.create` result | Rationale |
|--------------|----------------------|-----------|
| `ready` | **admit** — create the Run | the only state that can host a build |
| `torn_down`, `failed`, `crashed` | `stale_handle` | the bootable target is gone; the handle no longer denotes a usable System (the issue pins `torn_down → stale_handle`; `failed`/`crashed` are likewise dead) |
| `defined`, `provisioning` | `configuration_error` (`data.current_status`) | the System is live but not yet ready — a sequencing error; the agent should `jobs.wait`/`systems.get` first, then retry |

This splits "the System is gone" (`stale_handle`, terminal/dead) from "you are too
early" (`configuration_error`, recoverable by waiting), so the agent gets an actionable
distinction rather than one opaque rejection.

### 2. The binding invariant is enforced by deriving the Allocation through the System and rejecting a Run whose Allocation is terminal

`run.system → allocation` ([ADR-0003](0003-six-durable-objects.md)): a Run's Allocation
is **fixed by its System**, not named independently. The `runs` table accordingly stores
only `system_id` (no `allocation_id` column), so the binding is *structural* — a Run can
never disagree with its System about which Allocation it occupies.

On top of that structural guarantee, `runs.create` reads the System's Allocation and
admits the Run **only when that Allocation is `active`** — an **allowlist**
(`{ACTIVE}`), symmetric with the System check (`{READY}`), not a denylist of terminal
states. A `ready` System should only ever sit under an `active` Allocation (provisioning
flips `granted → active`), so requiring `active` both closes the transient
orphaned-System window ([ADR-0023](0023-discovery-allocation-admission.md):
`allocations.release` releases the Allocation but leaves System teardown to the
reconciler — a System can be `ready` while its Allocation is already `released`) **and**
rejects `releasing` and any future non-`active`-but-non-terminal Allocation state (M1
leasing) without a denylist that silently rots as states are added. A non-`active`
Allocation → `stale_handle`. The Allocation read is **lock-free** (no
`LockScope.ALLOCATION`): it catches the reachable already-released case without a third
lock; the residual race (a release that commits between the read and the Run insert)
leaves a Run on a System the reconciler is about to tear down — the same transient,
reconciler-covered state, recoverable by a new Run, not a corruption.

### 3. The `open → active` flip is atomic with the first Run insert, under a per-Investigation advisory lock; `last_run_at` is maintained on every `runs.create`

Creating a Run inserts the `runs` row **and** flips the Investigation `open → active`
(on its first Run) in **one transaction**, so the flip and the Run commit together or not
at all. The read-decide-flip runs under `advisory_xact_lock(INVESTIGATION,
investigation_id)`, so two concurrent first-Runs produce exactly **one** `open → active`
audit row: the first flips and commits, the second observes `active` and skips the
transition. Every `runs.create` (first or later) sets `last_run_at = now()`, so the
column reflects the most recent Run (the reconciler's idle-Investigation sweep, M1, reads
it).

`runs.create` holds **two** advisory locks (System and Investigation). To avoid deadlock,
the codebase fixes a global acquisition order — **`ALLOCATION → SYSTEM → INVESTIGATION →
RUN`** — and `runs.create` takes `SYSTEM` before `INVESTIGATION`, consistent with it.
`LockScope` gains an `INVESTIGATION` member; the lock-key derivation is unchanged.

### 4. External-ref identity is the `(tracker, id)` natural key; `link` upserts, `unlink` removes-if-present; both and `close` require a non-terminal Investigation

An `ExternalRef` is `{tracker, id, url}`; its identity is the **`(tracker, id)`** pair (the
same bug in the same tracker is one link, regardless of `url`). `investigations.link`
is an **idempotent upsert**: it removes any existing `(tracker, id)` match and appends the
supplied ref, so re-linking yields one entry and a changed `url` is a correction, not a
duplicate. `investigations.unlink` is an **idempotent remove-if-present**: unlinking an
absent `(tracker, id)` is a success no-op (the postcondition "not linked" holds), so a
retry is forward-progressing. Because the identity is `(tracker, id)`, `unlink`'s input
contract requires only `{tracker, id}` — a `url` is neither required nor consulted
(supplying one is accepted and ignored) — so a caller holding only the tracked-item
identity can unlink without fabricating the stored `url`. `link`, by contrast, takes the
full `{tracker, id, url}` because it writes the `url`.

`link`, `unlink`, and `close` mutate the Investigation only when it is **non-terminal**
(`open` / `active`); a `closed` / `abandoned` Investigation is immutable and these tools
return `configuration_error` (`data.current_status`) — **except** `close` on an
already-`closed` Investigation, which is an idempotent success (its postcondition already
holds). All four read-modify-write under `advisory_xact_lock(INVESTIGATION, id)` so a
concurrent mutation cannot lose an `external_refs` edit (read-modify-write on a jsonb
column is not atomic without it).

### 5. RBAC: mutations require `operator`; reads require project membership

`investigations.open` / `.close` / `.link` / `.unlink` and `runs.create` are operator
actions (`require_role(ctx, project, Role.OPERATOR)`), matching `allocations.request` /
`systems.provision`. `investigations.get` / `runs.get` require only project membership:
they read the row and return the **not-found-shaped** `configuration_error` when the
object's `project` is not in `ctx.projects`, so the tool never leaks the existence of
another project's Investigation/Run (the `systems.get` / `allocations.get` posture).
Authorization denials **raise** (`AuthorizationError` / `AuthError`) rather than returning
an envelope — the M0 taxonomy has no authorization `ErrorCategory`
([ADR-0020](0020-rbac-audit-gate-implementation.md)), as resolved for #14.

`runs.create` joins an Investigation and a System that **must share a project**: the Run's
`project` is `investigation.project`, and a System whose `project` differs is rejected with
`configuration_error` (a cross-project join is not a valid Run).

### 6. `runs.get` renders `failed` via the Run's `failure_category`; `build_profile` stays an opaque dict in M0

The Run state value `"failed"` collides with the response envelope's failure-status set
([ADR-0019](0019-tool-response-envelope.md): `_FAILURE_STATUSES = {"failed", "error"}`), so
`runs.get` renders a `failed` Run as a `failure` envelope carrying the Run's own
`failure_category` (the column the build/boot plane sets), defaulting to
`INFRASTRUCTURE_FAILURE` when it is unexpectedly absent. `canceled` is **not** a failure
status, so it renders as `success("canceled")`. No `runs.*` tool in #17 *produces*
`failed`/`canceled` (the build/install/boot plane does); `runs.get` renders them for
forward-compatibility.

`build_profile` is stored as opaque `jsonb` and is **not** validated or typed in #17 — the
`BuildProfile` model lands with the build plane that owns it, exactly as
`provisioning_profile` was opaque before [ADR-0024](0024-provisioning-profile-model-shape.md).
`runs.create` requires only that it is a JSON object.

### 7. `runs.create` is non-idempotent in M0 (deliberate)

Like `allocations.request` ([ADR-0023](0023-discovery-allocation-admission.md)),
`runs.create` is synchronous and carries no `dedup_key` or client idempotency token, so a
client retry inserts a **second** Run on the same `(investigation_id, system_id)`. This is
intentional: an Investigation legitimately groups many Runs on one System (the
retry-as-a-new-Run recovery model), so no natural uniqueness rule applies, and a duplicate
Run is **inert** — it sits in `created`, consuming no compute, until the agent issues a
`runs.build` step (a later plane), at which point the agent's own sequencing surfaces the
stray. The proper fix (a client idempotency token) is deferred to the build-plane work that
owns long-running step admission, where the `(run_id, step, kind)` `dedup_key` already
lives. Recorded so non-idempotent retries are a decision, not an oversight.

## Consequences

- A Run can only be created on a `ready` System whose Allocation is live, so the binding
  invariant holds at creation and a Run never points at a dead or orphaned System except
  through a transient, reconciler-covered race.
- The `open → active` flip is exactly-once under concurrency and commits with the Run; a
  crash mid-create leaves neither the Run nor the flip.
- `external_refs` edits are serialized per-Investigation, so concurrent `link`/`unlink`
  cannot lose an edit; `link`/`unlink` are idempotent, so client retries are safe.
- `LockScope` gains one member (`INVESTIGATION`) and the global lock order is documented;
  both are additive.
- `runs.get` correctly renders states (`failed`/`canceled`) that only a later plane
  produces, so that plane needs no `runs.get` change.
- `build_profile` carries no schema in M0; a malformed profile surfaces only when the
  build plane validates it, not at `runs.create`. Stated so the absent validation is a
  recorded decision, not an oversight.

## Considered & rejected

- **Admit a Run on any non-terminal System (permissive reading of the acceptance).**
  Rejected: a Run on a `provisioning` System would let `build`/`install` race the
  provision finalize; requiring `ready` matches the walking skeleton and gives the agent a
  clear `configuration_error` "wait, then retry" instead of a later, murkier failure. The
  acceptance pins only `torn_down → stale_handle`; ready-only is a strict superset that
  still satisfies it.
- **Map every non-`ready` System to one category.** Rejected: collapsing "gone"
  (`torn_down`/`failed`/`crashed`) and "too early" (`defined`/`provisioning`) loses the
  retry/abandon distinction the agent needs.
- **Take `LockScope.ALLOCATION` in `runs.create` to make the Allocation-terminal check
  race-free.** Rejected for M0: a third lock widens the deadlock surface and the
  acquisition-order rule for a check whose residual race is a transient state the
  reconciler already repairs. The lock-free read catches the real (already-released) case;
  the race window is documented.
- **Store `allocation_id` on the `runs` row.** Rejected: it would let a Run name an
  Allocation that disagrees with its System's, violating the binding invariant the schema
  is designed to make structural; the Allocation is derived through the System.
- **Check the binding with an Allocation *denylist* (`reject if released/failed`).**
  Rejected: a denylist admits every other state — including `releasing` and any future
  non-`active`-but-non-terminal state — so it rots silently as the Allocation lifecycle
  grows, and it is asymmetric with the System check, which is an allowlist (`{READY}`). The
  allowlist (`{ACTIVE}`) is correct today (a `ready` System has an `active` Allocation) and
  forward-safe.
- **`link`/`unlink` keyed on the full `{tracker, id, url}` triple.** Rejected: a `url`
  change would then create a duplicate `(tracker, id)` link and `unlink` would miss a ref
  whose `url` drifted; the `(tracker, id)` natural key models "one link per tracked item".
- **Make `link`/`unlink` reject a duplicate / absent ref with `configuration_error`.**
  Rejected: idempotent upsert/remove makes client retries safe and matches the additive,
  forgiving posture of `systems.teardown` on an already-torn-down System; a strict error
  buys nothing for an append-only ref list.
- **Allow `link`/`unlink` on a `closed`/`abandoned` Investigation.** Rejected: a terminal
  Investigation is immutable for auditability; record-keeping edits after close are not an
  M0 need. `close` on an already-`closed` Investigation is the one allowed terminal
  interaction (idempotent success), because its postcondition already holds.
- **Flip `open → active` lazily (e.g. in a read or the reconciler) instead of in
  `runs.create`.** Rejected: the spec ties the flip to the *first Run*; doing it anywhere
  but the Run insert opens a window where an Investigation with a Run is still `open`, and
  forces a second writer.
- **Skip `last_run_at`.** Rejected: the column exists for the M1 idle-Investigation sweep;
  populating it now is a one-line `SET` and avoids a backfill later.
