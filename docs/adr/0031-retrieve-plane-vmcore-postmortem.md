# ADR 0031 — Retrieve plane: vmcore capture/fetch + crash postmortem (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #24 (M0: Retrieve plane — vmcore capture/fetch + crash postmortem)
- **Depends on:** [ADR-0028](0028-control-plane-power-force-crash.md) (the `crashed`
  System and `force_crash` that produce a vmcore, and the realized-port + handler
  pattern this mirrors), [ADR-0029](0029-build-plane-local-make.md) (the `debuginfo_ref`
  on the Run this plane symbolizes against, and the seam-injected `live_vm`-gated
  provider shape `LocalLibvirtBuild` established), [ADR-0027](0027-safety-modules-secret-backend-impl.md)
  (the `Redactor` used to build the redacted derivative),
  [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (the artifact store, object key
  scheme, and `sensitive`/`redacted` sensitivity it writes; the object-before-row
  write order), [ADR-0018](0018-job-queue-worker-execution.md) (the job queue /
  `dedup_key` / worker the capture runs on),
  [ADR-0026](0026-investigation-run-lifecycle.md) (the Run join-point and `run_steps`
  ledger), [ADR-0019](0019-tool-response-envelope.md) (the response envelope).
- **Refines:** the M0 Retrieve wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md).
- **Spec:** [`../superpowers/specs/2026-06-04-retrieve-plane-design.md`](../superpowers/specs/2026-06-04-retrieve-plane-design.md)

## Context

A `force_crash` (ADR-0028) panics a guest configured with kdump (`CONFIG_CRASH_DUMP` +
`crashkernel`, preflighted by the build plane, ADR-0029); the guest's kdump kernel writes
a `vmcore` to a crash directory on the libvirt host. Issue #24 adds the **Retrieve
plane**: capture that vmcore, store it (raw + a redacted derivative) in the object store,
and symbolize it against the Run's `debuginfo_ref` for crash postmortem. The
`JobKind.CAPTURE_VMCORE` enum, the `ErrorCategory.READINESS_FAILURE` category, the
`artifacts` table, and the `Sensitivity` enum (`sensitive`/`redacted`) already exist; #24
adds the realized provider ports, the tools, the handlers, and the schema/registration
wiring. The decisions the parent spec leaves open are settled here.

## Decision

### 1. A realized `Retriever` port, seam-injected and `live_vm`-gated, mirroring `LocalLibvirtBuild`

The capture's slow, environment-bound operations — waiting for kdump to finish, reading
the raw vmcore bytes off the host, extracting the vmcore's build-id — are **injected
seams** that default to real implementations guarded by `# pragma: no cover - live_vm`,
exactly as `LocalLibvirtBuild` does for `make`/ELF reads. The redacted derivative is
produced by an injected `redact: Callable[[bytes], bytes]` seam defaulting to the
ADR-0027 `Redactor`-based reducer. So the orchestration and the full error contract are
unit-tested with fakes, and the real host/`crash`/`make` path runs only under the
existing `live_vm` gate.

```python
class CaptureOutput(NamedTuple): raw_ref: str; redacted_ref: str; vmcore_build_id: str
class Retriever(Protocol):
    def capture(self, run_id: UUID, system_id: UUID) -> CaptureOutput: ...
```

`capture()` waits up to a bounded window for a complete vmcore; if none appears it raises
`CategorizedError(READINESS_FAILURE)` (see §3). On success it writes **two** objects and
returns both refs plus the build-id.

### 2. Object-before-row, both objects before either row (ADR-0013 write order)

`capture()` writes the raw `sensitive` core and the `redacted` derivative to the object
store **first** (returning `StoredArtifact`s), and only then does the handler insert the
two `artifacts` rows in the finalize transaction. A capture re-dispatch with a recorded
`run_steps` ledger row skips the re-capture (the same dual-idempotency the build plane
uses: `dedup_key` bounds admission, the step ledger bounds execution). An orphaned object
from a crash between the two writes is bounded by object-store retention (ADR-0013); a row
without its object can never occur.

### 3. No complete vmcore within the capture window is `readiness_failure`, driving the Run `failed`

The acceptance pins it: "a no-core scenario returns `readiness_failure`". A capture that
finds no complete `vmcore` before the bounded window elapses (kdump never finished, or
produced only an incomplete `vmcore-incomplete`) raises
`CategorizedError(READINESS_FAILURE)`. The handler drives the Run `running → failed` with
that category and re-raises so the worker dead-letters the job — the same failure shape
the build handler uses, with the category the issue names. Distinct from
`INFRASTRUCTURE_FAILURE` (the object store or host became unreachable), which is retried.

### 4. `artifacts.get`/`.list` return the redacted derivative only; the raw core is never response-eligible

The redaction invariant (CLAUDE.md, ADR-0027) is made **structural**: `artifacts.list`
and `artifacts.get` filter the `artifacts` table to `sensitivity = 'redacted'`. A request
naming a `sensitive` artifact id is shaped as not-found (`configuration_error`), so the
raw vmcore cannot be fetched through the agent surface even by id — there is no code path
that returns `sensitive` bytes to a caller. The raw core exists only for the host-side
`crash` postmortem (which reads it from the store on the worker, never returns it).

### 5. `capture_vmcore` is admitted on a `crashed` System and keyed `{system_id}:capture_vmcore`

`capture_vmcore` takes a `run_id` (the Run whose `debuginfo_ref` the core symbolizes
against). It resolves the Run's System, requires `operator`, admits only when the System
is `crashed` (a `force_crash` ran), and enqueues `JobKind.CAPTURE_VMCORE` with
`dedup_key = f"{system_id}:capture_vmcore"` — the issue-named key — in one transaction
under the per-Run lock, flipping the Run `created|running → running` exactly as
`runs.build` does. The `{system_id}` key makes capture **once-per-System**: one System per
Allocation, one crash, one core. A retry returns the same job.

### 6. `vmcore.fetch` is a job (it stages bytes); `vmcore.list` and `artifacts.*` are synchronous reads

`vmcore.fetch` re-materializes the stored core into the object store under a
fetch-addressable key (a potentially slow object copy), so it returns a `{job_id}` handle
like every other long op (`JobKind.CAPTURE_VMCORE` reused — fetch is a re-stage of the
same captured core, keyed `{run_id}:vmcore_fetch`). `vmcore.list`, `artifacts.list`, and
`artifacts.get` are pure reads over the `artifacts` table (no provider work) and return
synchronously, isolating bad rows per the envelope's `*.list` contract.

### 7. A realized `CrashPostmortem` port for `postmortem.crash`/`.triage`, loading `debuginfo_ref`

`postmortem.crash`/`.triage` are **synchronous, ungated** offline reads (no destructive
op, no admission gate — matching v1, where the load-bearing control is the command
allowlist, not a gate). They resolve the Run, load its `debuginfo_ref` (the build plane's
`vmlinux`) and the captured raw core from the store, and run `crash -s <vmlinux> <vmcore>`
over an injected, `live_vm`-gated subprocess seam. Caller crash commands are validated
against the ported allowlist + metacharacter denylist (the v1 `commands.py` security
control) **before** any invocation. All output is run through the `Redactor` **before it
is returned and before it is persisted**. `postmortem.triage` composes a fixed crash
command batch into one report. The build-id of the core is verified against the
`debuginfo_ref`'s build-id (provenance match) before symbolizing; a mismatch is a
`configuration_error`.

### 8. The schema gains `JobKind.CAPTURE_VMCORE` capture/fetch payloads only; no new table

The `artifacts`, `runs`, `run_steps`, and `jobs` tables already carry everything the plane
needs. The only schema touch is **none** — `JobKind.CAPTURE_VMCORE` is already in the
`jobs_kind_check` constraint (it was added with the enum). The plane is additive in code:
two tool modules, one provider module, the handler registration tuple in `app.py`, and a
`LockScope` reuse (the per-Run lock already exists).

## Consequences

- The capture/fetch/postmortem logic is fully unit-testable with fake provider + fake
  store; the real host/`crash` path is `live_vm`-gated, so CI stays green without a
  toolchain or a libvirt host.
- The raw `sensitive` vmcore is never returned to an agent — `artifacts.*` are
  `redacted`-only by construction, not by per-call discipline.
- A no-core capture is a `readiness_failure` on the Run (the issue's acceptance); a store
  or host outage is a retryable `infrastructure_failure`.
- Object-before-row plus the step ledger give the same dual idempotency the build plane
  has: one capture per System (admission), one re-stage per job (execution).
- `mcp/app.py` gains one tuple append (the capture handler); `vmcore.py` registers the
  `vmcore.*`/`postmortem.*` tools and the capture handler; `artifacts.py` registers the
  `artifacts.*` reads. No schema migration.

## Considered & rejected

- **Return the raw `sensitive` vmcore through `artifacts.get` when an admin asks.**
  Rejected: the milestone has no per-artifact RBAC for raw guest memory, and the redaction
  invariant is surface-wide. Making `artifacts.*` `redacted`-only by construction removes
  the raw core from every response path; the raw core is reachable only by the host-side
  postmortem on the worker, never as a tool result. A privileged raw-fetch path returns
  with per-artifact authorization in a later milestone.
- **Capture the vmcore synchronously inside `control.force_crash`.** Rejected: kdump can
  take minutes to write a multi-GB core, and `force_crash` is an admission that enqueues a
  job (ADR-0028) — blocking it on the capture would couple two long ops and break the
  "admit fast, work async" contract. Capture is its own job, admitted after the System is
  observed `crashed`.
- **Key the capture job `{run_id}:capture_vmcore`.** Rejected: the issue names
  `(system_id, "capture_vmcore")`, and the core is a property of the crashed *System* (one
  per Allocation), not of a particular Run — two Runs on the same crashed System would
  capture the same core. The `{system_id}` key makes capture once-per-System; the Run is
  carried in the payload for the `debuginfo_ref` symbolization target.
- **Port the v1 manifest/`ArtifactStore`/`SSH runner` stack wholesale.** Rejected: v2
  stores artifacts in S3 keyed by object id (ADR-0013), runs the provider on the worker
  host (no SSH tier in M0 local-libvirt — the host *is* local), and records steps in the
  `run_steps` ledger, not a JSON manifest. The port keeps the v1 *logic* (command
  allowlist, build-id provenance check, dump enumeration/selection, redaction-before-
  return) and re-homes it on the v2 store/queue/seam patterns.
- **Make `postmortem.crash` a gated destructive op.** Rejected: it is a read-only offline
  inspection of a captured core; v1 deliberately left it ungated, with the command
  allowlist as the security boundary. Adding a gate would mis-model a read as destructive
  and over-constrain triage.
- **Add a dedicated `vmcores` table.** Rejected: a captured core is an `artifacts` row
  (owner_kind `runs`, the two refs distinguished by `sensitivity` and `name`); a separate
  table would duplicate the artifact lifecycle the store already owns.
