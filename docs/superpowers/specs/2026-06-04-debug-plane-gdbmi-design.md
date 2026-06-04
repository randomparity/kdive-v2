# Debug plane: gdb-MI tier (M0) — design

- **Issue:** #21 (M0: Debug plane: port gdb-MI tier)
- **ADR:** [ADR-0034](../../adr/0034-debug-plane-gdbmi-tier.md)
- **Depends on:** #20 (Connect plane + DebugSession lifecycle, merged) — its
  `debug.start_session`/`debug.end_session` tools, the `DebugSession`
  `attach → live → detached` row, and the `TransportHandleData` (`gdbstub://host:port`)
  handle this plane keys its live gdb/MI engine on.
- **Status:** Draft

## 1. Problem

A `live` `DebugSession` (#20) records that a single-attach gdbstub transport is open to a
booted, `ready` System. #20 returns **no** guest bytes — it only proves the stub answers
RSP framing. Issue #21 adds the **Debug plane**: constrained, agent-initiated debug
operations over that stub via a ported gdb-MI tier, exposing seven tools:

- `debug.set_breakpoint(session_id, location)` — hardware breakpoint at a bare C symbol
- `debug.clear_breakpoint(session_id, number)` — delete a breakpoint by gdb id
- `debug.list_breakpoints(session_id)` — enumerate current breakpoints
- `debug.read_memory(session_id, address, byte_count)` — read ≤ **4096** bytes, **verbatim**
- `debug.read_registers(session_id, registers)` — read named registers
- `debug.continue(session_id, timeout_sec)` — resume, wait for a stop (or interrupt back)
- `debug.interrupt(session_id)` — SIGINT the inferior back to a known stop

These extend the **existing** `src/kdive/mcp/tools/debug.py` (#20 created it). The gdb-MI
engine is ported from v1 `providers/local/debug/gdb_mi.py` (~854 LOC) into a new
`src/kdive/providers/local_libvirt/debug_gdbmi.py`.

## 2. Acceptance criteria (live_vm)

From the issue, the live acceptance is: set a breakpoint at a symbol, continue, hit it,
read registers and ≤4096 bytes of memory; a >4096 read is **rejected**; a known secret in
the gdb-MI **textual transcript** is masked in the response.

**The redaction distinction (load-bearing):** transcript/record *text* (the JSON-lines
transcript file, breakpoint/register/stop records returned in the envelope) passes through
the `Redactor` before persistence and before it is returned. Raw `read_memory` **bytes** are
returned **verbatim** under the 4096-byte cap — they are NOT run through the redactor (the
redactor is a text/structure masker; binary guest memory is opaque bytes, and silently
mutating them would corrupt the very dump the agent asked for). The cap is the memory-read
safety control; the redactor is the transcript-text safety control. They are independent.

The real socket/gdb-subprocess path is `live_vm`-gated; CI green requires no gdb/KVM host.

## 3. Scope boundary — what ports, what does not

The v1 engine is broad (854 LOC: attach, symbol resolution, module symbol loading, stack
inspection, watchpoints, evaluate, postmortem note scanning). Issue #21's scope is the
**seven tools above only.** This spec ports the engine surface those seven need and
**drops** the out-of-scope v1 surface rather than carrying dead code (global rule: no
phantom features). Specifically:

**Ported (in scope):**
- `GdbMiAttachment` (controller + transcript path + accumulated records) and the
  injectable `MiController` Protocol + `PygdbmiController` real impl (`live_vm`).
- `MiRecord`/`Frame`/`StopRecord`/`BreakpointRef` typed records and `parse_mi_records`.
- `set_breakpoint` (hardware `-break-insert -h`), `clear_breakpoint`, `list_breakpoints`.
- `read_registers` (name→ordinal map via `-data-list-register-names`).
- `read_memory` (4096 cap; returns **bytes** verbatim).
- `continue_`/`interrupt` and the `_ExecutionControl` resume/wait/interrupt machinery
  (mi-async, timeout → `-exec-interrupt` fallback, `transport_stall`).
- The redacted transcript append and per-record redaction.
- `GdbMiSessionRegistry` (in-process `session_id` → `GdbMiAttachment`) plus the
  per-session `asyncio.Lock` table (§4a).

**Not ported (out of #21 scope — drop, do not carry as dead code):**
- `attach`/`probe_read`/`resolve_symbol`/`load_module_symbols`/`read_symbol`/
  `evaluate_inspector`/`backtrace`/`list_variables`/`set_watchpoint`/`step`/`next`/`finish`.
  These belong to the Connect-plane attach (#20, already done differently) and to later
  introspection/postmortem milestones. They are **not** added here; if a later issue needs
  them, it ports them then.

**Rationale for the trim** is recorded as an ADR-0034 rejected alternative ("port the whole
854-LOC engine verbatim").

## 4. Where the live attachment comes from (the #20 ↔ #21 seam)

#20's `start_session` returns a `live` `DebugSession` whose `transport_handle` is a
serialized `TransportHandleData` (`gdbstub://host:port`). #20 does **not** open a gdb/MI
subprocess — its `Connector` only probes RSP reachability and returns the endpoint.

The gdb/MI engine is **lazily attached on first Debug-plane op** and cached in a
process-scoped `GdbMiSessionRegistry` keyed on `session_id`. The first `debug.*` op for a
session:
1. loads the `DebugSession` row, authorizes (operator, project), checks `state == live`;
2. takes the **per-session `asyncio.Lock`** for that `session_id` (§4a);
3. looks up the live `GdbMiAttachment` in the registry by `session_id`;
4. on a miss, **opens** a gdb/MI subprocess and attaches over the session's RSP endpoint
   (decoded from `transport_handle`), registering the attachment; on a hit, reuses it;
5. dispatches the blocking engine op (§4b) and releases the lock.

This keeps #20 unchanged (no schema, no new column) and makes the engine
server-process-scoped and non-durable: a server restart strands the in-process attachment,
and the next op gets `no_live_session` (`CONFIGURATION_ERROR`, `data["code"]="no_live_session"`,
§5a) — the agent must `debug.end_session` + `debug.start_session` to re-establish. This is
the v1 `ADR 0021` in-process-registry contract, ported.

### 4a. The attach-or-create and every op are serialized per session (`asyncio.Lock`)

The gdbstub is **single-attach** (the whole #20 premise), and one `GdbMiAttachment` is a
single stateful gdb subprocess with one mutable record list + transcript. So two concurrent
`debug.*` ops on one `live` session must not (a) both miss the registry and both spawn a gdb
subprocess against the one stub, nor (b) both drive the one engine and interleave its
records/transcript. The plane keeps a process-scoped `dict[session_id, asyncio.Lock]` (guarded
by a short non-async lock for the get-or-create of the `Lock` itself); each handler holds the
per-session lock across the **attach-or-create + the single engine op**. Only one op ever
attaches; ops on the same session are serialized; ops on *different* sessions proceed in
parallel. A lost double-attach race therefore cannot happen — the second arrival finds the
attachment already registered under the lock and reuses it.

### 4b. Blocking engine ops are dispatched via `asyncio.to_thread`

Every engine op drives a blocking gdb subprocess write/read, and `debug.continue` waits up to
`MAX_INTERACTIVE_WAIT_SEC` (60s) for a stop. The handlers are `async def` (matching #20), and
the FastMCP server runs them on one event loop. As every other v2 plane does for synchronous
provider IO (`runs.build`, `vmcore.capture`, `control.power` all use `await
asyncio.to_thread(...)`), each blocking engine call is dispatched with `await
asyncio.to_thread(...)` so a 60s `continue` never stalls the event loop or other sessions.
The per-session `asyncio.Lock` (§4a) guarantees only one thread ever touches a given
attachment, so the `MiController` needs no internal locking.

### 4c. The attach seam takes the RSP endpoint **and** a debuginfo source; both are `live_vm`-gated

The v1 attach loaded kernel symbols via `-file-exec-and-symbols <vmlinux>`. v2 has **no**
vmlinux path on the `DebugSession` row (it carries only `transport_handle`) and stores
debuginfo as an **object-store key** produced by the Build plane (#29 `runs.build` stores a
`vmlinux`/debuginfo object). The attach seam signature is therefore
`attach(rsp_endpoint, debuginfo_ref) -> GdbMiAttachment`, where `debuginfo_ref` is resolved
from the session's Run the same way the postmortem/retrieve plane (#31) resolves the
debuginfo object for `crash`: from the Run's succeeded `build` step output. **In M0 (no live
host) the resolver raises `MISSING_DEPENDENCY`** (surfaced as `DEBUG_ATTACH_FAILURE`), so the
resolution + attach is the single `live_vm`-gated seam and the error contract is unit-tested
with a fake attach seam that returns a `GdbMiAttachment` over a fake `MiController`. Resolving
and materializing the debuginfo object to a local file is the seam's `live_vm` work;
everything above it (handler authz, the per-session lock, registry lookup, every engine op
against the fake `MiController`) is unit-tested off the gate.

### 4d. `end_session` reaps the engine — the engine lifecycle is bound to the session lifecycle

#20's `end_session` only drives the row `→ detached` and calls the (no-op) `close_transport`;
it knows nothing of the gdb/MI engine #21 introduces. With #21 spawning a **real gdb
subprocess** on first op, an ended session would otherwise strand that subprocess **and** its
RSP attach — and because the gdbstub is single-attach, a leaked attach makes every *future*
`start_session` on the same System fail `transport_conflict` forever, while gdb processes
accumulate. So `end_session` is extended (in the same `debug.py`) to, under the per-session
lock, `reap(session_id)` the registry entry and `controller.exit()` the engine
(best-effort, never blocking the detach — exactly as #20's `_close` is best-effort), and drop
the per-session `asyncio.Lock` from the lock table (bounding its growth). Reaping a session
with no live engine (the common case — most sessions never run a Debug-plane op) is a no-op.
This is the v1 `GdbMiSessionRegistry.reap()` contract, ported and wired to `end_session`.
The crash/reboot `live → detached` path (#23's `_detach_sessions`) runs in a **different
process** from the one holding the engine in M0 (the worker vs. the MCP server), so it cannot
reap the in-process engine; that stranded-engine case is the `no_live_session` path (§5a) the
next op on a since-detached session would hit anyway — the row is already `detached`, so the
state gate rejects it as `not_live` first.

## 5. Tool surface and contracts

All seven handlers follow the established envelope contract (ADR-0019): they return
`ToolResponse.success/failure` with the most specific `ErrorCategory` and literal
`suggested_next_actions`. They are **synchronous** (no `JobKind`) — a breakpoint/read/resume
is a bounded MI round-trip, exactly as ADR-0032 §3 classifies debug ops.

| tool | engine op | success status | suggested_next_actions |
|------|-----------|----------------|------------------------|
| `debug.set_breakpoint` | `set_breakpoint` (`-break-insert -h`) | `set` | `["debug.continue", "debug.list_breakpoints"]` |
| `debug.clear_breakpoint` | `clear_breakpoint` (`-break-delete`) | `cleared` | `["debug.list_breakpoints"]` |
| `debug.list_breakpoints` | `list_breakpoints` (`-break-list`) | `listed` | `["debug.set_breakpoint", "debug.continue"]` |
| `debug.read_memory` | `read_memory` (cap 4096, bytes) | `read` | `["debug.read_registers", "debug.continue"]` |
| `debug.read_registers` | `read_registers` | `read` | `["debug.read_memory", "debug.continue"]` |
| `debug.continue` | `continue_` (resume→wait/interrupt) | `stopped` | `["debug.read_registers", "debug.read_memory", "debug.list_breakpoints"]` |
| `debug.interrupt` | `interrupt` | `stopped` | `["debug.read_registers", "debug.continue"]` |

**Authorization & state gate (every handler, shared helper):**
- `session_id` must parse as a UUID → else `CONFIGURATION_ERROR`.
- `DebugSession` row must exist and `session.project ∈ ctx.projects` → else
  `CONFIGURATION_ERROR` (same opacity as #20: a cross-project / missing id is a config error,
  not an authz leak of existence).
- `require_role(ctx, project, Role.OPERATOR)`.
- `session.state` must be `live` → else `CONFIGURATION_ERROR` (`current_status`). A
  `detached`/`attach` session has no live engine to drive.

**Input validation (ported invariants, re-enforced at the handler/engine boundary):**
- breakpoint `location`: bare C identifier (`^[A-Za-z_][A-Za-z0-9_]*$`) → else
  `CONFIGURATION_ERROR`. Keeps `-break-insert` non-injectable.
- breakpoint `number`: bare integer (`^[0-9]+$`) → else `CONFIGURATION_ERROR`.
- register names: non-empty list of `^[A-Za-z][A-Za-z0-9_]*$` → else `CONFIGURATION_ERROR`.
- `read_memory`: `1 ≤ byte_count ≤ 4096` and `0 ≤ address ≤ 0xFFFFFFFFFFFFFFFF` → else
  `CONFIGURATION_ERROR`. **A `byte_count > 4096` is rejected before any MI command runs.**
- `continue` `timeout_sec`: bounded to `[1, MAX_INTERACTIVE_WAIT_SEC]` (rounded up), as v1.

**Error mapping:** the engine raises `CategorizedError` (the v1 `GdbMiError` is folded into
the existing `CategorizedError` — no new exception type, per the global "replace, don't
deprecate" rule). Handlers catch it and emit `ToolResponse.failure(session_id, exc.category)`.
The engine's categories already align with the M0 taxonomy: `CONFIGURATION_ERROR`,
`DEBUG_ATTACH_FAILURE`, `INFRASTRUCTURE_FAILURE`, `MISSING_DEPENDENCY`.

### 5a. `CONFIGURATION_ERROR` variants carry a machine-readable `data["code"]`

Several distinct failures share `CONFIGURATION_ERROR`, but the agent's recovery differs, so
each carries a discriminating `data["code"]` (the same pattern #20 used with
`reason="boot_first"`). The agent branches on `code`, never on the free-text message:

| `data["code"]` | trigger | agent recovery |
|----------------|---------|----------------|
| `bad_session_id` | `session_id` is not a UUID | fix the argument |
| `unknown_session` | row absent or cross-project | the id is wrong |
| `not_live` | session state ≠ `live` (`data["current_status"]`) | `debug.start_session` a new session |
| `no_live_session` | registry miss after a server restart stranded the engine | `debug.end_session` then `debug.start_session` |
| `bad_location` / `bad_breakpoint_id` / `bad_register` / `bad_read_range` | input validation | fix the argument |

`AUTHORIZATION_DENIED` (non-operator) and the attach categories
(`DEBUG_ATTACH_FAILURE`/`MISSING_DEPENDENCY`) are already distinct categories and need no
sub-code.

## 6. read_memory: bytes verbatim under the cap (the critical invariant)

`debug.read_memory` returns the raw guest bytes, hex-decoded from the gdb/MI
`-data-read-memory-bytes` `memory=[{contents:...}]` segments and concatenated, capped at
4096 bytes. These bytes are **not** redacted. The envelope cannot carry raw bytes in a
`dict[str, str]` `data` field directly, so the response surfaces the bytes as a lowercase
hex string under `data["memory_hex"]` plus `data["byte_count"]` and `data["address"]` — the
hex is a faithful, lossless rendering of the verbatim bytes (no masking). The 4096 cap is
the only constraint applied. A `byte_count > 4096` request never reaches gdb: it is a
`CONFIGURATION_ERROR` at the handler boundary.

The transcript line for the read command **is** redacted (it is text), but the returned
`memory_hex` bytes are not — the acceptance test asserts both: a secret in a *record/transcript*
text field is masked, while requested memory bytes round-trip unchanged.

## 7. Redaction boundary

The engine owns a `Redactor` built fresh per attachment. Every place v1 redacts is
preserved:
- `_append_transcript` redacts the whole JSON-lines entry before writing to the
  per-session transcript file (text path).
- `BreakpointRef`, `StopRecord`, and the `read_registers` result are run through
  `redact_value` before they become part of a response.
- `read_memory`'s **bytes** are the explicit exception (§6).

`Redactor` is the merged #25 `kdive.security.redaction.Redactor`; it seeds from
`PROCESS_SECRET_REGISTRY`, so a registered secret appearing in any transcript/record text is
masked without the call site re-supplying it.

## 8. Testing strategy

Handlers are the unit of testing — call them directly with an injected pool and an injected
**attach seam** that returns a `GdbMiAttachment` wrapping a scripted **fake `MiController`**
(no gdb subprocess, no socket). The fake controller maps each MI command string to a canned
list of pygdbmi record dicts, so every engine op is driven deterministically. Tests cover:

- happy path per tool (set/clear/list breakpoint, read registers, read memory, continue,
  interrupt) returning the right status + `suggested_next_actions`;
- **the 4096 boundary**: `byte_count == 4096` succeeds, `byte_count == 4097` is a
  `CONFIGURATION_ERROR` with no MI command issued;
- **bytes verbatim**: a `read_memory` whose `contents` hex decodes to bytes that *look like* a
  secret string come back unchanged in `memory_hex` (not masked);
- **transcript redaction**: a registered secret appearing in a breakpoint/stop record text
  field is `[REDACTED]` in the returned envelope and in the transcript file;
- input-validation rejections (bad symbol, bad bp id, empty/bad register name, out-of-range
  address) → `CONFIGURATION_ERROR`, no MI command;
- state/authz rejections (missing session, cross-project, non-operator, non-`live` state),
  asserting the §5a `data["code"]` discriminator on each `CONFIGURATION_ERROR` variant;
- `no_live_session` after a registry miss when the (faked) attach seam reports the engine is
  gone (`data["code"]="no_live_session"`);
- engine error mapping (`^error` MI result → `DEBUG_ATTACH_FAILURE`; MI write timeout →
  `INFRASTRUCTURE_FAILURE` `transport_stall`; continue-timeout→interrupt fallback path);
- **attach-or-create runs once under the per-session lock**: a fake attach seam that counts
  invocations is called exactly once across two ops on the same session, and the second op
  reuses the registered attachment (no second spawn);
- the `MISSING_DEPENDENCY` debuginfo-resolver default (M0, no live host) surfaces as
  `DEBUG_ATTACH_FAILURE` through a real-resolver-but-faked path;
- **`end_session` reaps the engine**: after a Debug-plane op registers a fake engine,
  `debug.end_session` calls the fake controller's `exit()` and drops the registry entry, and
  a subsequent op on the (now `detached`) session is rejected at the state gate. `end_session`
  on a session that never ran a Debug-plane op still succeeds (reap is a no-op).

`asyncio.to_thread` dispatch (§4b) is exercised implicitly — the fake `MiController` is
plain blocking code, so the handler under test runs it through the real `to_thread` path.

The `PygdbmiController` real subprocess and the real attach (gdb spawn + RSP connect) are
`live_vm`-gated and `# pragma: no cover - live_vm`.

## 9. Files

- **new** `src/kdive/providers/local_libvirt/debug_gdbmi.py` — the trimmed gdb-MI engine,
  records, `MiController`/`PygdbmiController`, `GdbMiSessionRegistry`, attach seam.
- **edit** `src/kdive/mcp/tools/debug.py` — add the seven handlers + their `@app.tool`
  registrations to the existing `register(app, pool)`; build one process-scoped
  `GdbMiSessionRegistry` + per-session lock table + attach seam there and inject into
  handlers; dispatch each blocking engine op via `await asyncio.to_thread(...)`; extend the
  existing `end_session` to reap + `exit()` the engine under the per-session lock (§4d).
- **edit** `pyproject.toml` — add `pygdbmi==0.11.0.0`.
- **new** `tests/providers/local_libvirt/test_debug_gdbmi.py` — engine + record tests.
- **edit** `tests/mcp/test_debug_tools.py` — Debug-plane handler tests (added to the existing
  start/end-session test file).
- **edit (minimal, shared with #22)** `docs/adr/README.md` — add the ADR-0034 row;
  `tests/mcp/test_app.py` — assert the seven new tool names register.
- **new** `docs/adr/0034-debug-plane-gdbmi-tier.md`.

No schema migration (the engine is in-process, keyed on the existing `transport_handle`;
no new column or `JobKind`). `app.py` needs **no** new `_PLANE_REGISTRARS` entry — the debug
plane's `register` already runs (#20); we extend it.
