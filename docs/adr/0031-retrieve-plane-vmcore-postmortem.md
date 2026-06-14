# ADR 0031 — Retrieve plane: vmcore capture/fetch + crash postmortem (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #24 (M0: Retrieve plane — vmcore capture/fetch + crash postmortem)
- **Depends on:** [ADR-0028](0028-control-plane-power-force-crash.md) (the `crashed`
  System and `force_crash` that produce a vmcore, and the realized-port + handler +
  per-System advisory-lock pattern this mirrors),
  [ADR-0029](0029-build-plane-local-make.md) (the `debuginfo_ref` on the Run this plane
  symbolizes against, and the seam-injected `live_vm`-gated provider shape
  `LocalLibvirtBuild` established),
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the `Redactor` used to build
  the redacted derivative), [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (the artifact store, object key
  scheme, `sensitive`/`redacted` sensitivity, and the object-before-row write order),
  [ADR-0018](0018-job-queue-worker-execution.md) (the job queue / `dedup_key` / worker
  the capture runs on, and the terminal Job success/failure semantics capture records
  on), [ADR-0026](0026-investigation-run-lifecycle.md) (the Run that carries
  `debuginfo_ref`), [ADR-0019](0019-tool-response-envelope.md) (the response envelope).
- **Refines:** the M0 Retrieve wording in
  [`../specs/m0-walking-skeleton.md`](../design/m0-walking-skeleton.md) (the
  `vmcore.list(system_id)` / `vmcore.fetch(system_id)` surface and the
  `sensitive`/`redacted` artifact rule).
- **Spec:** [`../superpowers/specs/2026-06-04-retrieve-plane-design.md`](../archive/superpowers/specs/2026-06-04-retrieve-plane-design.md)

## Context

A `force_crash` (ADR-0028) panics a guest configured with kdump (`CONFIG_CRASH_DUMP` +
`crashkernel`, preflighted by the build plane, ADR-0029); the guest's kdump kernel writes
a `vmcore` to a crash directory on the libvirt host. Issue #24 adds the **Retrieve
plane**: capture that vmcore, store it (raw + a redacted derivative) in the object store,
expose it for fetch/listing, and symbolize it against the Run's `debuginfo_ref` for crash
postmortem. The `JobKind.CAPTURE_VMCORE` enum, the `ErrorCategory.READINESS_FAILURE`
category, the `artifacts` table, and the `Sensitivity` enum (`sensitive`/`redacted`)
already exist; #24 adds the realized provider ports, the tools, the handler, and the
registration wiring. The `jobs_kind_check` constraint already lists `capture_vmcore`, so
**no schema migration is needed**. The decisions the parent spec leaves open are settled
here.

## Decision

### 1. The vmcore is System-scoped; `vmcore.fetch(system_id)` is the kdump-capture op, and `capture_vmcore` is its JobKind (not a separate tool)

The walking-skeleton surface is `vmcore.list(system_id)` and `vmcore.fetch(system_id) →
{job_id} # waits for kdump capture`. The vmcore is a property of the crashed **System**
(one System per Allocation, one crash), not of a Run. So `vmcore.fetch` is the admission
that enqueues `JobKind.CAPTURE_VMCORE` (dedup `{system_id}:capture_vmcore`); the
`capture_vmcore` handler is the work behind it. Issue #24's "register `capture_vmcore`
handler" and "`vmcore.fetch` (→ job)" are the same operation viewed from the two seams —
there is no separate `capture_vmcore` *tool*. The artifacts are owned `owner_kind=systems`,
`owner_id=system_id`.

### 2. Capture moves no durable-object lifecycle state; it records on the Job and the artifact rows

A Run that has built a kernel is already `RunState.SUCCEEDED`, a **terminal** state
(`domain/state.py`); capture happens after `force_crash`, so it can transition no Run. The
System is already `crashed` and capture neither advances nor regresses it. Capture is
therefore a System-scoped artifact-production job that **writes no `state` column**:

- The **Job** carries success/failure (ADR-0018): `succeeded` with the raw core key as
  `result_ref`, or `failed` with an `error_category` — the worker's normal terminal
  handling. A no-core capture raises `CategorizedError(READINESS_FAILURE)`, which the
  worker records as the job's `error_category`; nothing else is mutated.
- The two `artifacts` rows are capture's only other durable output.

This is the deliberate departure from the build plane's "drive the Run `running →
succeeded`" finalization: there is no Run lifecycle slot for a System-scoped capture, so
capture must not borrow one.

### 3. A realized `Retriever` port, seam-injected and `live_vm`-gated, mirroring `LocalLibvirtBuild`

Waiting for kdump, reading the raw core, extracting its build-id, and producing the
redacted derivative are **injected seams** defaulting to real implementations guarded by
`# pragma: no cover - live_vm`. So the orchestration and the full error contract are
unit-tested with fakes; the real host/`crash`/`makedumpfile` path runs only under the
existing `live_vm` gate.

```python
class CaptureOutput(NamedTuple): raw: StoredArtifact; redacted: StoredArtifact; vmcore_build_id: str
class Retriever(Protocol):
    def capture(self, system_id: UUID) -> CaptureOutput: ...
```

`capture()` writes the raw `sensitive` core and the `redacted` derivative to the object
store, returning both `StoredArtifact`s plus the build-id; if no complete core appears in
the bounded window it raises `READINESS_FAILURE` (see §5).

### 4. Object-before-row, both objects before either row; execution idempotency via a lock-guarded existence check (no schema change)

`capture()` writes both objects **first**; the handler then inserts the two `artifacts`
rows in one transaction under the **per-System advisory lock** (`LockScope.SYSTEM`, the
lock every System mutation already holds). Execution idempotency: the handler, holding the
lock, first checks whether a `vmcore` row already exists for the System and, if so, returns
its key without re-capturing. Because the lock serializes all per-System work and the
object key is deterministic (`{tenant}/systems/{system_id}/vmcore`), a worker re-dispatch
re-puts the same key (idempotent S3 overwrite) and the existence check prevents a duplicate
row — **no `artifacts` unique constraint and no migration are required**. An orphaned object
from a crash between the two object writes is bounded by object-store retention (ADR-0013);
a row without its object can never occur.

### 5. No complete vmcore within the capture window is `readiness_failure` (the job's error_category)

The acceptance pins it. A capture that finds no complete `vmcore` before the bounded window
elapses (kdump never finished, or produced only `vmcore-incomplete`) raises
`CategorizedError(READINESS_FAILURE)`; the worker dead-letters the job with that category.
Distinct from `INFRASTRUCTURE_FAILURE` (the object store or host became unreachable, or the
System row vanished), which the worker retries.

### 6. `artifacts.get`/`.list` are redacted-only; the raw core is never response-eligible

`artifacts.list(system_id)` and `vmcore.list(system_id)` filter to `sensitivity =
'redacted'`. `artifacts.get(artifact_id)` returns the row only when it is `redacted`; a
`sensitive` id is shaped as not-found (`configuration_error`), indistinguishable from a
missing one — so the raw vmcore cannot be fetched through the agent surface even by id. The
agent reaches the derivative by listing (which surfaces only `redacted` rows) and getting
that id. The skeleton's "`artifacts.get` on a sensitive object returns the redacted
derivative" describes a transparent sensitive→redacted swap; M0 has no sensitive→redacted
mapping column, so the redacted derivative is reached as its **own** row, and the
transparent swap is deferred (see considered-and-rejected). The raw core is reachable only
by the host-side postmortem on the worker, never as a tool result.

### 7. A realized `CrashPostmortem` port for `postmortem.crash`/`.triage`, loading the Run's `debuginfo_ref`

`postmortem.crash`/`.triage` are **synchronous, ungated** offline reads (no destructive op,
no admission gate — matching v1, where the security boundary is the command allowlist, not
a gate). They take a `run_id` to load its `debuginfo_ref` (the build plane's `vmlinux`),
resolve the Run's System, load that System's captured raw core from the store, and run
`crash -s <vmlinux> <vmcore>` over an injected, `live_vm`-gated subprocess seam. Caller
crash commands are validated against the ported allowlist + metacharacter denylist (the v1
`commands.py` control) **before** any invocation. The core's build-id is verified against
the `debuginfo_ref`'s build-id (provenance) before symbolizing; a mismatch is a
`configuration_error`. All output is run through the `Redactor` **before it is returned and
before it is persisted**. `postmortem.triage` composes a fixed crash command batch into one
report. A Run whose `debuginfo_ref` is null (not yet built) is a `configuration_error`.

## Consequences

- Capture is System-scoped and lifecycle-neutral: it writes no `state` column, records on
  the Job and the artifact rows, and cannot collide with the terminal Run lifecycle.
- The capture/fetch/postmortem logic is fully unit-testable with fake provider + fake
  store; the real path is `live_vm`-gated, so CI stays green without a toolchain or host.
- The raw `sensitive` vmcore is never returned to an agent — `artifacts.*` are
  `redacted`-only by construction.
- A no-core capture is a `readiness_failure` job; a store/host/System-gone fault is a
  retryable `infrastructure_failure`.
- Idempotency without a migration: `dedup_key` bounds admission (one capture per System),
  the lock-guarded existing-row check bounds execution (one set of rows per System).
- `mcp/app.py` gains one tuple append (the capture handler); `vmcore.py` registers the
  `vmcore.*`/`postmortem.*` tools and the capture handler; `artifacts.py` registers the
  `artifacts.*` reads. **No schema migration.**

## Considered & rejected

- **Key the capture on `run_id` / drive the Run lifecycle.** Rejected: the skeleton surface
  is `vmcore.fetch(system_id)`, and a built Run is already `RunState.SUCCEEDED` (terminal),
  so capture has no Run state to move and two Runs on the same crashed System would capture
  the same core. Capture is System-scoped and lifecycle-neutral; postmortem takes a
  `run_id` only to read `debuginfo_ref`.
- **A separate `capture_vmcore` tool distinct from `vmcore.fetch`.** Rejected: the skeleton
  makes `vmcore.fetch(system_id) → {job_id}` "wait for kdump capture" — `capture_vmcore` is
  that job's *kind*, not a second tool. One admission, one handler.
- **Reuse `JobKind.CAPTURE_VMCORE` for a separate `vmcore` "fetch/re-stage" op.** Rejected:
  the worker dispatches one handler per kind, so a fetch job and a capture job sharing the
  kind would dispatch to the same handler and need payload-shape branching. `vmcore.fetch`
  *is* the capture op (it returns the captured core's ref); there is no separate re-stage in
  M0, so no second use of the kind and no ambiguity.
- **Add a `(owner_kind, owner_id, object_key)` unique constraint for `ON CONFLICT`
  idempotency.** Rejected: that is a migration on a shared schema file, and the per-System
  advisory lock plus a deterministic object key already make the handler's existing-row
  check sufficient. Avoiding the constraint keeps #24 migration-free and off the shared
  `0001_init.sql` that a sibling issue is editing.
- **Return the raw `sensitive` vmcore through `artifacts.get` for an admin.** Rejected: M0
  has no per-artifact RBAC for raw guest memory, and the redaction invariant is surface-
  wide. `artifacts.*` are `redacted`-only by construction; a privileged raw-fetch path
  returns with per-artifact authorization later.
- **Implement the transparent sensitive→redacted swap on `artifacts.get` now.** Rejected:
  it needs a sensitive→redacted mapping the `artifacts` row does not encode in M0; adding
  it is schema + lookup scope #24 does not own. The derivative is reached as its own
  (`redacted`) row via `list` + `get`; the swap returns with per-artifact scope later.
- **Store a redacted full-core image as the derivative.** Rejected: scrubbing every guest
  memory page is expensive and out of M0 scope. The M0 derivative is the **redacted dmesg
  text** extracted from the core (safe to return); the raw core stays `sensitive` and
  host-only. A redacted core image returns with the drgn/introspection tier.
- **Capture the vmcore synchronously inside `control.force_crash`.** Rejected: kdump can
  take minutes to write a multi-GB core, and `force_crash` admits-and-enqueues (ADR-0028);
  blocking it would couple two long ops. Capture is its own job, admitted via
  `vmcore.fetch` after the System is observed `crashed`.
- **Make `postmortem.crash` a gated destructive op.** Rejected: it is a read-only offline
  inspection; v1 left it ungated with the command allowlist as the boundary. A gate would
  mis-model a read as destructive.
- **Add a dedicated `vmcores` table.** Rejected: a captured core is an `artifacts` row
  (owner_kind `systems`, the raw/derivative distinguished by `sensitivity` + object name); a
  separate table would duplicate the artifact lifecycle the store already owns.
