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

## Surface

| Tool | Args | Returns | Sync/Job |
|------|------|---------|----------|
| `capture_vmcore` | `run_id` | `{job_id}` handle (carries `run_id`, `system_id`) | Job (`JobKind.CAPTURE_VMCORE`, dedup `{system_id}:capture_vmcore`) |
| `vmcore.list` | `run_id` | list of artifact envelopes (the captured cores for the Run) | Sync read |
| `vmcore.fetch` | `run_id` | `{job_id}` handle (re-stages the core) | Job (dedup `{run_id}:vmcore_fetch`) |
| `artifacts.list` | `run_id` | list of **redacted-only** artifact envelopes | Sync read |
| `artifacts.get` | `artifact_id` | one **redacted** artifact envelope (ref) | Sync read |
| `postmortem.crash` | `run_id`, `commands` | crash batch output (redacted) | Sync, ungated |
| `postmortem.triage` | `run_id` | composed triage report (redacted) | Sync, ungated |

All return `ToolResponse` (ADR-0019). Handlers are the unit of test (called directly with
injected pool + provider). RBAC: `capture_vmcore` requires `operator`; reads require
project membership; `postmortem.*` require project membership (ungated, no destructive op).

## Components

### `providers/local_libvirt/retrieve.py`

```python
class CaptureOutput(NamedTuple): raw_ref: str; redacted_ref: str; vmcore_build_id: str
class Retriever(Protocol):
    def capture(self, run_id: UUID, system_id: UUID) -> CaptureOutput: ...

class CrashOutput(NamedTuple): results: dict[str, object]; transcript: str; truncated: bool
class CrashPostmortem(Protocol):
    def run(self, *, vmcore_ref: str, debuginfo_ref: str, commands: list[str]) -> CrashOutput: ...
```

`LocalLibvirtRetrieve` realizes both, seam-injected exactly like `LocalLibvirtBuild`:

- **capture seams:** `wait_for_vmcore(system_id) -> Path | None` (bounded poll of the
  crash dir; `None` = window elapsed with no complete core), `read_vmcore(Path) -> bytes`,
  `read_vmcore_build_id(bytes) -> str`, and a `redact: Callable[[bytes], bytes]` derivative
  reducer (ADR-0027 `Redactor`, applied to the dmesg/text-bearing portion; see §"Redacted
  derivative"). `store_factory` builds the `ObjectStore` lazily from env.
- **crash seams:** `run_crash(vmlinux: Path, vmcore: Path, script: str) -> CrashResult`
  (a `live_vm` subprocess: `prlimit … timeout … crash -s <vmlinux> <vmcore>` fed the
  command script on stdin), plus `fetch_object(ref) -> bytes` to stage inputs from the
  store onto the worker.

The orchestration (window handling, two-object store, build-id provenance check, command
validation, redaction-before-return) is host-free and unit-tested; the seams are
`# pragma: no cover - live_vm`.

`capture()` contract:
1. `path = wait_for_vmcore(system_id)`; `if path is None: raise CategorizedError(READINESS_FAILURE)`.
2. `data = read_vmcore(path)`; `build_id = read_vmcore_build_id(data)`.
3. `raw = store.put_artifact(tenant, "runs", run_id, "vmcore", data=data, sensitivity=SENSITIVE, retention_class="vmcore")`.
4. `redacted = store.put_artifact(tenant, "runs", run_id, "vmcore-redacted", data=redact(data), sensitivity=REDACTED, retention_class="vmcore")`.
5. return `CaptureOutput(raw.key, redacted.key, build_id)`.

A store failure at step 3/4 propagates `INFRASTRUCTURE_FAILURE` (retryable). The objects
are written before any row (ADR-0013); the handler inserts both `artifacts` rows after.

### `mcp/tools/vmcore.py`

- `capture_vmcore(pool, ctx, run_id)` — resolve Run (project-scoped), require `operator`,
  resolve System, admit only when `System.state is CRASHED` (else `configuration_error`
  with `current_status`), flip Run `created|running → running` and enqueue
  `JobKind.CAPTURE_VMCORE` (dedup `{system_id}:capture_vmcore`) in one per-Run-locked
  transaction; return the job handle carrying `run_id`/`system_id`.
- `capture_handler(conn, job, retriever)` — load Run; if a `run_steps` `capture_vmcore`
  ledger row exists, reuse its result (skip re-capture); else
  `await asyncio.to_thread(retriever.capture, run_id, system_id)`; on
  `CategorizedError` drive Run `running → failed(category)` and re-raise; on success,
  in one per-Run-locked finalize transaction: insert the two `artifacts` rows
  (`register_artifact_row`), record the `capture_vmcore` ledger row, write the Run
  `running → succeeded`-equivalent step completion, audit. Return the raw ref as
  `result_ref`.
- `vmcore_list(pool, ctx, run_id)` — read the Run's `vmcore`-named artifacts; envelope
  each, isolating bad rows.
- `vmcore_fetch(pool, ctx, run_id)` — admit a re-stage job (dedup `{run_id}:vmcore_fetch`);
  `vmcore_fetch_handler` re-puts the stored core under a fetch key and returns its ref.
- `postmortem_crash(pool, ctx, run_id, commands)` — resolve Run + `debuginfo_ref`,
  validate `commands` against the ported allowlist/denylist (synchronous
  `configuration_error` on a bad command), run the `CrashPostmortem` port over the core,
  **redact** the output, return it. Ungated.
- `postmortem_triage(pool, ctx, run_id)` — run a fixed crash command batch, compose a
  report, redact, return.
- `register(app, pool)` / `register_handlers(registry, *, retriever=None)`.

### `mcp/tools/artifacts.py`

- `artifacts_list(pool, ctx, run_id)` — `SELECT … FROM artifacts WHERE owner_kind='runs'
  AND owner_id=%s AND sensitivity='redacted'`; envelope each (ref = object_key),
  isolating bad rows.
- `artifacts_get(pool, ctx, artifact_id)` — fetch one artifact by id; if absent **or**
  `sensitivity != 'redacted'`, return `configuration_error` (not-found-shaped, so a raw
  `sensitive` id is indistinguishable from a missing one). Project membership enforced via
  the owning Run.
- `register(app, pool)`.

### Redacted derivative

The raw vmcore is binary kernel memory; a byte-level `Redactor.redact_text` pass over the
whole core is meaningless and slow. The redacted *derivative* for M0 is the **redacted
crash-relevant text** extracted from the core: the `redact` seam decodes the dmesg/log
portion (the `live_vm` real impl uses `crash`/`makedumpfile --dump-dmesg`) and runs it
through the ADR-0027 `Redactor` before storing. In the unit test the seam is a fake that
returns redacted text bytes. This keeps a `redacted` artifact that is safe to return
(`artifacts.get`) while the raw core stays `sensitive` and host-only.

## Error contract

| Condition | Category |
|-----------|----------|
| malformed `run_id`/`artifact_id`, unknown Run/artifact, System not `crashed`, bad crash command, build-id provenance mismatch | `configuration_error` |
| no complete vmcore within the capture window | `readiness_failure` |
| object store / host unreachable mid-capture or mid-fetch | `infrastructure_failure` |
| requested artifact exists but is `sensitive` | `configuration_error` (not-found-shaped) |
| caller lacks `operator` (`capture_vmcore`) / project membership | authz raises (no category, ADR-0020) |

## Idempotency

- **Admission:** `dedup_key={system_id}:capture_vmcore` — one capture per crashed System;
  a retry returns the same job. `vmcore.fetch` keyed `{run_id}:vmcore_fetch`.
- **Execution:** the `run_steps` `capture_vmcore` ledger row makes a worker re-dispatch
  reuse the stored refs without re-capturing (object-before-row + ledger, mirroring the
  build plane).

## Testing

Handlers/ports unit-tested with a `_FakeRetriever`/`_FakeCrashPostmortem` and a `_FakeStore`
(echoing keys), the migrated DB fixture, and direct handler calls — never through MCP.
Edges: no-core window → `readiness_failure` drives Run `failed`; store failure →
`infrastructure_failure`; System not `crashed` → `configuration_error`; `artifacts.get` on
a `sensitive` id → not-found-shaped; bad crash command → synchronous `configuration_error`;
build-id mismatch → `configuration_error`; capture re-dispatch reuses the ledger; redaction
applied before the response and before persistence; `register_handlers` binds
`CAPTURE_VMCORE`. The real host/`crash`/`makedumpfile` path is `live_vm`-gated.

## Out of scope

Per-artifact RBAC for raw-core fetch (deferred); drgn introspection tier (a later issue);
SSH-tier remote retrieval (M0 local-libvirt host is local).
