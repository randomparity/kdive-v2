# Retrieve plane: vmcore capture/fetch + crash postmortem — design

- **Issue:** #24 (M0: Retrieve plane)
- **ADR:** [ADR-0031](../../adr/0031-retrieve-plane-vmcore-postmortem.md)
- **Date:** 2026-06-04
- **Depends on (merged):** #23 (control: `crashed` System, `force_crash`), #25
  (redaction / secret backend), #8 (object store), #18 (build plane: `debuginfo_ref`).

## Goal

Capture the kdump vmcore a crashed System produces, store it (raw `sensitive` + a
`redacted` derivative) in the object store, expose it for fetch/listing, and symbolize it
against the Run's `debuginfo_ref` for crash postmortem — all on the v2 store/queue/seam
patterns, with the real host/`crash` path `live_vm`-gated.

## Canonical surface (from `m0-walking-skeleton.md`)

The walking-skeleton spec fixes the Retrieve surface and keys it on **`system_id`**, not
`run_id` — the vmcore is a property of the crashed System (one per Allocation), not of a
Run:

```
Retrieve    vmcore.list(system_id) → [{artifact_ref}]
            vmcore.fetch(system_id) → {job_id}   # waits for kdump capture → vmcore artifact
            artifacts.list(run_id|system_id) / .get(artifact_ref)
```

`vmcore.fetch` **is** the kdump-capture op: it enqueues a `JobKind.CAPTURE_VMCORE` job
that waits for kdump to finish, stores the core, and returns its artifact ref. Issue #24's
"register `capture_vmcore` handler" is that job's handler; "`vmcore.fetch` (→ job)" is its
admission. There is no separate `capture_vmcore` *tool* — `capture_vmcore` is the
**JobKind**.

## Surface

| Tool | Args | Returns | Sync/Job |
|------|------|---------|----------|
| `vmcore.fetch` | `system_id` | `{job_id}` handle (carries `system_id`) | Job (`JobKind.CAPTURE_VMCORE`, dedup `{system_id}:capture_vmcore`) |
| `vmcore.list` | `system_id` | list of the System's vmcore artifact envelopes (redacted-eligible) | Sync read |
| `artifacts.list` | `system_id` | list of the System's **redacted-only** artifact envelopes | Sync read |
| `artifacts.get` | `artifact_id` | one **redacted** artifact envelope (ref) | Sync read |
| `postmortem.crash` | `run_id`, `commands` | crash batch output (redacted) | Sync, ungated |
| `postmortem.triage` | `run_id` | composed triage report (redacted) | Sync, ungated |

All return `ToolResponse` (ADR-0019). Handlers are the unit of test (called directly with
injected pool + provider). RBAC: `vmcore.fetch` requires `operator`; reads require project
membership; `postmortem.*` require project membership (ungated, no destructive op).

`postmortem.*` take a `run_id` because they need the Run's `debuginfo_ref` (the
build-plane `vmlinux`) to symbolize; they resolve the Run's System and use that System's
captured vmcore. Capture itself never touches a Run.

## State ownership (capture is System-scoped; it moves no object lifecycle)

A Run that has built a kernel is already `RunState.SUCCEEDED` — a **terminal** state
(`domain/state.py`); capture happens after `force_crash` and cannot transition a Run.
Capture is therefore a System-scoped artifact-production job that **moves no durable
object's lifecycle state**:

- The **Job** carries capture's success/failure: it reaches `succeeded` with a
  `result_ref` (the raw core key) or `failed` with an `error_category` (the worker's
  normal terminal handling, ADR-0018) — no Run/System state is written.
- The System stays `crashed` (it is already `crashed`; capture neither advances nor
  regresses it).
- Idempotent execution is bounded by the artifact rows themselves (see §Idempotency), not
  a Run-step ledger.

This is the key correction over a build-plane-style "drive the Run `running → succeeded`"
shape: there is no Run lifecycle slot for capture, so capture records on the Job and the
`artifacts` rows alone.

## Components

### `providers/local_libvirt/retrieve.py`

```python
class CaptureOutput(NamedTuple): raw: StoredArtifact; redacted: StoredArtifact; vmcore_build_id: str
class Retriever(Protocol):
    def capture(self, system_id: UUID) -> CaptureOutput: ...

class CrashOutput(NamedTuple): results: dict[str, object]; transcript: str; truncated: bool
class CrashPostmortem(Protocol):
    def run(self, *, vmcore_ref: str, debuginfo_ref: str, commands: list[str]) -> CrashOutput: ...
```

`LocalLibvirtRetrieve` realizes both, seam-injected exactly like `LocalLibvirtBuild`:

- **capture seams:** `wait_for_vmcore(system_id) -> bytes | None` (bounded poll of the
  crash dir, returning the complete core's bytes; `None` = window elapsed with no complete
  core), `read_vmcore_build_id(bytes) -> str`, and `extract_redacted(bytes) -> bytes`
  (the redacted derivative reducer; see §"Redacted derivative"). `store_factory` builds the
  `ObjectStore` lazily from env.
- **crash seams:** `run_crash(vmlinux: Path, vmcore: Path, script: str) -> CrashResult`
  (a `live_vm` subprocess: `prlimit … timeout … crash -s <vmlinux> <vmcore>` fed the
  command script on stdin), plus `fetch_object(ref) -> bytes` to stage inputs from the
  store onto the worker.

The orchestration (window handling, two-object store, build-id provenance check, command
validation, redaction-before-return) is host-free and unit-tested; the seams are
`# pragma: no cover - live_vm`.

`capture()` contract:
1. `data = wait_for_vmcore(system_id)`; `if data is None: raise CategorizedError(READINESS_FAILURE)`.
2. `build_id = read_vmcore_build_id(data)`.
3. `raw = store.put_artifact(tenant, "systems", system_id, "vmcore", data=data, sensitivity=SENSITIVE, retention_class="vmcore")`.
4. `redacted = store.put_artifact(tenant, "systems", system_id, "vmcore-redacted", data=extract_redacted(data), sensitivity=REDACTED, retention_class="vmcore")`.
5. return `CaptureOutput(raw, redacted, build_id)`.

A store failure at step 3/4 propagates `INFRASTRUCTURE_FAILURE` (retryable). Both objects
are written before either `artifacts` row (ADR-0013); the handler inserts the rows after.

### `mcp/tools/vmcore.py`

- `fetch_vmcore(pool, ctx, system_id)` — resolve System (project-scoped), require
  `operator`; admit only when the System is in a state that can have produced a core
  (`crashed`, or terminal `torn_down`/`failed` if a row already exists — see below);
  enqueue `JobKind.CAPTURE_VMCORE` (dedup `{system_id}:capture_vmcore`) and return the job
  handle carrying `system_id`. A System that never crashed (`ready`/`provisioning`/…) is a
  `configuration_error` with `current_status`.
- `capture_handler(conn, job, retriever)` — re-resolve the System under the per-System
  advisory lock; if it is no longer present, `INFRASTRUCTURE_FAILURE`. **Idempotency:** if
  a `vmcore` artifact row already exists for the System, return its key (no re-capture).
  Otherwise `await asyncio.to_thread(retriever.capture, system_id)`; on `CategorizedError`
  re-raise (the worker dead-letters the job with the category — `readiness_failure` for
  no-core); on success insert the two `artifacts` rows (`register_artifact_row`) in one
  transaction and return the raw ref as `result_ref`. Inserting the `vmcore`-named raw row
  is gated by a unique `(owner_kind, owner_id, object_key)` check / `ON CONFLICT DO
  NOTHING` so a re-dispatch after the objects were stored but before commit is safe.
- `list_vmcores(pool, ctx, system_id)` — read the System's `vmcore`/`vmcore-redacted`
  artifacts; envelope each, isolating bad rows.
- `postmortem_crash(pool, ctx, run_id, commands)` — resolve Run + its `debuginfo_ref` and
  System; load the System's raw `vmcore` ref; validate `commands` against the ported
  allowlist/denylist (synchronous `configuration_error` on a bad command); run the
  `CrashPostmortem` port; **redact** the output; return it. Ungated. A Run with no
  `debuginfo_ref` (not yet built) or a System with no captured core is a
  `configuration_error`.
- `postmortem_triage(pool, ctx, run_id)` — run a fixed crash command batch, compose a
  report, redact, return.
- `register(app, pool)` / `register_handlers(registry, *, retriever=None)`.

### `mcp/tools/artifacts.py`

- `artifacts_list(pool, ctx, system_id)` — `SELECT … FROM artifacts WHERE
  owner_kind='systems' AND owner_id=%s AND sensitivity='redacted'`; envelope each
  (ref = object_key), isolating bad rows.
- `artifacts_get(pool, ctx, artifact_id)` — fetch one artifact by id; if absent **or**
  `sensitivity != 'redacted'`, return `configuration_error` (not-found-shaped, so a raw
  `sensitive` id is indistinguishable from a missing one — see "Redacted-only" below).
  Project membership enforced via the owning System.
- `register(app, pool)`.

### Redacted-only resolution vs. the skeleton's "returns the redacted derivative"

The skeleton (`m0-walking-skeleton.md` §Object store) says `artifacts.get` on a
`sensitive` object "returns the redacted derivative". The redacted derivative is a
**separate `artifacts` row** (the `vmcore-redacted` object), with its own id. So
`artifacts.get` does not transparently swap a `sensitive` id for its derivative (which
would require a sibling-lookup the row does not encode in M0); it instead returns the
redacted row when called with the redacted id, and treats a `sensitive` id as
not-found-shaped. The agent reaches the derivative through `artifacts.list`/`vmcore.list`
(which surface only the `redacted` rows) and then `artifacts.get(redacted_id)`. This
honors "only a redacted derivative is response-eligible" without a sensitive→redacted
mapping column M0 has not designed; the transparent-swap form returns with per-artifact
scope in a later milestone.

### Redacted derivative

The raw vmcore is binary kernel memory; a byte-level `Redactor.redact_text` pass over the
whole core is meaningless and slow. The **M0 redacted derivative is the redacted
crash-relevant text** extracted from the core (the dmesg/log ring buffer), **not** a
redacted full-core image: the `extract_redacted` seam decodes the dmesg portion (the
`live_vm` real impl uses `crash`/`makedumpfile --dump-dmesg`) and runs it through the
ADR-0027 `Redactor` before storing. A redacted *full-core image* (scrubbing guest memory
pages) is deferred — see the ADR's considered-and-rejected. In the unit test the seam runs
the **real `Redactor`** over attacker-controlled text (e.g. a planted `password=hunter2`),
so the test asserts redaction actually occurred, not merely that bytes were returned.

## Error contract

| Condition | Category |
|-----------|----------|
| malformed `system_id`/`run_id`/`artifact_id`, unknown System/Run/artifact, System never crashed, Run not built (`debuginfo_ref` null), bad crash command, build-id provenance mismatch | `configuration_error` |
| no complete vmcore within the capture window | `readiness_failure` |
| object store / host unreachable mid-capture; System row gone at capture time | `infrastructure_failure` |
| requested artifact exists but is `sensitive` | `configuration_error` (not-found-shaped) |
| caller lacks `operator` (`vmcore.fetch`) / project membership | authz raises (no category, ADR-0020) |

## Idempotency

- **Admission:** `dedup_key={system_id}:capture_vmcore` — one capture per crashed System;
  a `vmcore.fetch` retry returns the same job (in whatever state it has reached). The
  `{system_id}` key makes capture once-per-System (one System per Allocation, one crash).
- **Execution:** the `vmcore` `artifacts` row is the execution ledger — a worker
  re-dispatch finds the existing row and returns its key without re-capturing (object-
  before-row, ADR-0013). The raw-row insert uses `ON CONFLICT DO NOTHING` on
  `(owner_kind, owner_id, object_key)` so the deterministic System-keyed object key makes
  the second insert a no-op. No Run-step ledger is involved (capture is not a Run step).

## Testing

Handlers/ports unit-tested with a `_FakeRetriever`/`_FakeCrashPostmortem` and a `_FakeStore`
(echoing keys), the migrated DB fixture, and direct handler calls — never through MCP.
Edges: no-core window → `readiness_failure` (job dead-letters); store failure →
`infrastructure_failure`; System never crashed → `configuration_error`; `artifacts.get` on
a `sensitive` id → not-found-shaped; redacted id → redacted ref; bad crash command →
synchronous `configuration_error`; build-id provenance mismatch → `configuration_error`;
Run with null `debuginfo_ref` → `configuration_error`; capture re-dispatch finds the
existing `vmcore` row and skips re-capture; redaction asserted over planted secret content
before the response and before persistence; System gone at capture → `infrastructure_failure`;
`register_handlers` binds `CAPTURE_VMCORE`. The real host/`crash`/`makedumpfile` path is
`live_vm`-gated.

## Out of scope

Per-artifact RBAC for raw-core fetch and the transparent sensitive→redacted swap on
`artifacts.get` (both deferred); a redacted full-core image (M0 ships redacted dmesg text);
drgn introspection tier (a later issue); SSH-tier remote retrieval (M0 local-libvirt host
is local).
