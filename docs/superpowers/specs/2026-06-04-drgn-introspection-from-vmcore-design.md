# Debug plane: drgn introspection from vmcore (offline) — design

- **Issue:** #22 (M0: Debug plane — drgn introspection from vmcore, offline)
- **ADR:** [ADR-0033](../../adr/0033-drgn-introspection-from-vmcore.md)
- **Date:** 2026-06-04
- **Depends on (merged):** #24 (retrieve plane: the captured `vmcore` artifact +
  `debuginfo_ref`/`build_id` resolution this reuses verbatim), #18 (build plane:
  `debuginfo_ref` = the Run's `vmlinux`), #25 (redaction).

## Goal

Run drgn over a **captured vmcore on the host** (offline — no live guest, no SSH),
loading the Run's `debuginfo_ref` (`vmlinux`) for symbols/types, and return a minimal
helper set (tasks, modules, sysinfo). All output is redacted before it is returned and
before it is persisted. The real drgn call is `live_vm`-gated; the orchestration, the
provenance check, the helper dispatch, and the redaction are unit-tested with a fake
drgn program — so this plane has real, non-gated tests with no `live_vm` host.

Live drgn (`introspect.run`, drgn-over-SSH against a booted guest) is **deferred to
M1** and is not implemented here.

## Canonical surface

`m0-walking-skeleton.md` lists the Debug plane's offline introspection as
`introspect.from_vmcore`. The M0 subset is a single tool:

```
Debug    introspect.from_vmcore(run_id) → {tasks, modules, sysinfo}  # offline, redacted
```

It keys on **`run_id`** (not `system_id`): the tool needs the Run's `debuginfo_ref`
(the build-plane `vmlinux`) to load symbols/types, exactly as `postmortem.crash` does.
It resolves the Run's System and uses that System's captured raw `vmcore`. Introspection
itself moves no durable-object lifecycle state — it is a synchronous, ungated offline
read, like `postmortem.crash`.

## Surface

| Tool | Args | Returns | Sync/Job |
|------|------|---------|----------|
| `introspect.from_vmcore` | `run_id` | `{tasks, modules, sysinfo}` (redacted JSON) | Sync, ungated |

`introspect.from_vmcore` returns a `ToolResponse` (ADR-0019). The structured helper
output is JSON-serialized into `data["report"]` (a string), because `ToolResponse.data`
is `dict[str, str]`. RBAC: project membership only — ungated, no destructive op, no
admission gate (matching `postmortem.*`; the security boundary here is that the script
is **not caller-supplied** — only the three fixed helpers run).

The handler is the unit of test (called directly with an injected pool + fake
introspector), never through MCP.

## Why no caller-supplied script in M0

v1's `introspect.from_vmcore` accepts an arbitrary user drgn `script` and renders it into
a sandboxed wrapper (`render_vmcore_wrapper`, base64 path encoding, byte caps, a subprocess
`timeout`). That is a large security surface (arbitrary code execution against kernel
memory) and an entire wrapper-rendering/parsing subsystem. The M0 acceptance asks only for
"a minimal helper set (tasks, modules, sysinfo)". So M0 runs **only the three fixed,
in-tree helper scripts** — there is no caller-supplied script, no wrapper rendering, no
per-call byte cap or `timeout` subprocess. The arbitrary-script path (and its wrapper/cap
machinery) returns with the live introspection tier in M1, where the script-execution
surface is designed as a whole.

## State ownership

Introspection is a synchronous read that **moves no durable object's lifecycle state**
and writes no artifact row by default (the acceptance asks only that it *returns* the
data, redacted). It records nothing on the Run, the System, or a Job. (Persisting a
redacted introspection report as an `artifacts` row is deferred — see Out of scope. The
"redacted before persistence" requirement is satisfied because redaction happens before
the value leaves the port, so any later persistence is of already-redacted text.)

## Components

### `providers/local_libvirt/introspect_drgn.py`

The drgn open + helper dispatch behind a typed seam, mirroring `LocalLibvirtRetrieve`'s
`CrashPostmortem`:

```python
class IntrospectOutput(NamedTuple):
    tasks: dict[str, object]
    modules: dict[str, object]
    sysinfo: dict[str, object]

class VmcoreIntrospector(Protocol):
    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput: ...
```

`LocalLibvirtVmcoreIntrospect` realizes it, seam-injected exactly like `LocalLibvirtRetrieve`:

- **`fetch_object(ref) -> bytes`** stages the raw core and the `vmlinux` from the object
  store onto the worker (the same seam `LocalLibvirtRetrieve` already uses; reused).
- **`read_vmcore_build_id(bytes) -> str`** reads the core's GNU build-id for the provenance
  check (reused from `retrieve.py`, see §provenance).
- **`open_program(vmcore: Path, vmlinux: Path) -> _Program`** is the **`live_vm`-gated**
  drgn seam: it imports `drgn`, calls `program_from_core`-style open against the staged
  core, and loads `vmlinux` for symbols/types. `_Program` is a narrow typed `Protocol`
  (the subset the helpers call) so drgn — which may be ty-unresolvable — is confined to
  this one seam and a single `# ty: ignore[unresolved-import]` at the import line.
- **`run_helper(program, name) -> dict[str, object]`** executes one of the three fixed
  helper scripts against the opened program and returns a structured dict.

`from_vmcore()` contract:
1. `vmcore_bytes = fetch_object(vmcore_ref)`.
2. `observed = read_vmcore_build_id(vmcore_bytes)`; `if observed != expected_build_id:
   raise CategorizedError(CONFIGURATION_ERROR)` (provenance — identical to
   `LocalLibvirtRetrieve.run`).
3. stage `vmcore_bytes` and `fetch_object(debuginfo_ref)` to temp files.
4. `program = open_program(core_file, vmlinux_file)`; an open failure raises
   `CategorizedError(DEBUG_ATTACH_FAILURE)`.
5. run the three helpers; assemble `IntrospectOutput(tasks, modules, sysinfo)`.

The orchestration (provenance, temp staging, dispatch) is host-free and unit-tested; the
`open_program`/`run_helper`/`read_vmcore_build_id` real impls are `# pragma: no cover -
live_vm`. `fetch_object`'s real impl reads the store (already gated in `retrieve.py`).

If the `open_program`/`run_helper` seams were not configured (the default `from_env`
real seams raise), the port raises `CategorizedError(MISSING_DEPENDENCY)` — the same shape
`LocalLibvirtRetrieve.run` uses when its crash seams are absent.

### The three helpers (ported M0 subset of v1 `introspect/helpers/`)

`tasks`, `modules`, `sysinfo` — ported from v1 `introspect/helpers/{tasks,modules,sysinfo}.py`.
Each is a drgn script body operating on a `prog` (the opened `Program`) and emitting a dict:

- **`tasks`** — process list (pid/tgid/comm/state) + kernel stacks, focused on blocked
  (D-state) tasks, bounded by a `limit`. (v1 `tasks.py`.)
- **`modules`** — loaded modules (name/size/refcount/used_by/state) with a `decode_errors`
  counter; an all-failed decode raises. (v1 `modules.py`.)
- **`sysinfo`** — uts fields (release/version/machine/nodename), boot cmdline, online-CPU
  and total-RAM-page counters. (v1 `sysinfo.py`.)

In M0 these run **in-process** against the opened `Program` (no subprocess wrapper), so
the helper bodies become small typed functions over the `_Program` protocol rather than
string scripts piped to `python3 -`. A helper that raises mid-decode degrades to an
error marker in its own sub-dict (e.g. `{"error": "<type>"}`) rather than failing the
whole call, **except** `modules`' all-failed case which the v1 helper raises on (a
fully-wrong decode path) — that becomes a `DEBUG_ATTACH_FAILURE` for the whole call.

### `mcp/tools/introspect.py`  (NOT `debug.py` — see ADR §placement)

- `introspect_from_vmcore(pool, ctx, run_id, introspector)` —
  resolve the Run + its `debuginfo_ref` and System; read the build plane's recorded
  build-id from the Run's `build` `run_steps` result (`result["build_id"]`); load the
  System's raw `vmcore` object key. (This resolution is the same `_resolve_postmortem`
  shape `vmcore.py` already uses; the shared parts are reused, not re-derived.) Then call
  `introspector.from_vmcore(...)`; on `CategorizedError` return a typed failure (never a
  500); **redact** the assembled report via the ADR-0027 `Redactor` and return it as
  `data["report"]` (JSON string). A Run with null `debuginfo_ref` (not built), no recorded
  `build` step result, or a System with no captured core is a `configuration_error`.
- `register(app, pool)` — registers the `introspect.from_vmcore` tool, building the
  introspector lazily from env (no drgn import at registration).

There is **no** `register_handlers` — introspection is synchronous, not a job kind.

### Shared-file edits (kept minimal; concurrent with sibling #20)

- `mcp/app.py` — one entry appended to `_PLANE_REGISTRARS` (`introspect.register`) and one
  import. No `_HANDLER_REGISTRARS` change (no job kind).
- `docs/adr/README.md` — one index row for ADR-0033.
- `tests/mcp/test_app.py` — only if it asserts the registered tool set; the new tool name
  is added there if so.

## Provenance (identical to `postmortem.crash`)

drgn loading the wrong `vmlinux` against a core yields silently wrong symbols. So before
running any helper the port verifies the captured core's GNU build-id equals the build-id
the build plane recorded for the Run (`run_steps` `build` result `build_id`, written by
ADR-0029 §5). A mismatch is a `configuration_error` — the exact provenance gate
`LocalLibvirtRetrieve.run` already enforces, reusing `read_vmcore_build_id`.

## Redaction (before return AND before any persistence)

The helper output can carry guest-derived strings (`comm`, module names, kernel-stack
frames, the boot cmdline, uts `version`). The assembled report is run through the
ADR-0027 `Redactor.redact_value` (structure-aware) **before it leaves the port / is
returned**, so any later persistence is of already-redacted text. The unit test plants a
secret-shaped token (e.g. a `comm` of `token=hunter2`) in the fake program's output and
asserts it is `[REDACTED]` in the response.

## Error contract

| Condition | Category |
|-----------|----------|
| malformed `run_id`, unknown Run, Run not built (`debuginfo_ref` null), no recorded `build` step result, System with no captured core | `configuration_error` |
| captured core build-id ≠ the Run's recorded build-id (provenance) | `configuration_error` |
| drgn cannot open the core / load the vmlinux; `modules` all-failed decode | `debug_attach_failure` |
| the drgn seams are not configured on the introspector (default real seams) | `missing_dependency` |
| caller lacks project membership | authz raises (no category, ADR-0020) |

## Testing

The handler + port are unit-tested with a `_FakeIntrospector` / a `_FakeProgram` and the
migrated DB fixture, called directly — never through MCP. Edges: Run with null
`debuginfo_ref` → `configuration_error`; no recorded build step → `configuration_error`;
System with no captured core → `configuration_error`; build-id provenance mismatch →
`configuration_error`; drgn open failure → `debug_attach_failure`; `modules` all-failed
decode → `debug_attach_failure`; a single helper raising mid-decode degrades to an error
marker (not a whole-call failure); a planted secret-shaped guest string is `[REDACTED]`
in the returned report (redaction asserted over attacker-controlled content); the
port runs the **real `Redactor`** so the test proves redaction, not mock theater;
`register` adds the tool. The real drgn open/helper path is `live_vm`-gated and never run
in CI.

## Out of scope

- **Live drgn (`introspect.run`)** — drgn-over-SSH against a booted guest; deferred to M1
  (needs the guest SSH transport + secret backend the M0 walking skeleton does not
  otherwise require; `introspect.run` is not in the M0 tool subset).
- **Caller-supplied drgn scripts** and the v1 wrapper-rendering/byte-cap/`timeout`
  subprocess machinery (return with the live tier in M1).
- **Persisting a redacted introspection report** as an `artifacts` row (M0 returns it;
  redaction already happens before any persistence, so adding a row later is row-insertion
  only).
- The remaining v1 helpers (`dmesg`, `irq`, `slab`) — M0 ships the three the acceptance
  names; the rest return with the live tier.
- **`check_prerequisites` / `drgn_probe`** — that path probes a **live target** over SSH
  for drgn/debuginfo readiness; the offline-vmcore M0 path has no live target to probe, so
  the probe (v1 `prereqs/drgn_probe.py`) is not ported here. Its `normalize_build_id`
  helper is subsumed by the build-id provenance the port already does via
  `read_vmcore_build_id`.
