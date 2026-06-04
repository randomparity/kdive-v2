# Connect plane (gdbstub) + DebugSession lifecycle — design (M0, #20)

- **Status:** Draft
- **Date:** 2026-06-04
- **Issue:** #20 (M0: Connect plane (gdbstub) + DebugSession lifecycle)
- **ADR:** [ADR-0032](../../adr/0032-connect-plane-gdbstub-debugsession.md)
- **Depends on (merged):** #13 (provisioning/discovery), #19 (response envelope /
  artifacts), #25 (control plane #23 / ADR-0028 — the `force_crash` `live → detached`
  edge and the `debug_sessions` join-through-`runs` pattern).

## Problem

A booted System exposes a QEMU gdbstub. The agent needs to open a single-attach
transport to it and own a durable `DebugSession` row across the attachment. Issue #20
adds the **Connect plane** (`open_transport(system, "gdbstub")`) and the two
session-lifecycle tools `debug.start_session` / `debug.end_session`. The debug
*operations* (breakpoints, memory/register reads) are #22's Debug plane — out of scope
here; this issue ships only the transport open/close and the session row.

Everything the lifecycle needs already exists on `main`:

- `DebugSession` model (`run_id`, `state`, `transport`, `transport_handle`,
  `worker_heartbeat_at`) and `DEBUG_SESSIONS` repository.
- `DebugSessionState` (`attach → live → detached`, `attach → detached`) and the
  transition guard.
- The `debug_sessions` table + `debug_sessions_state_check` constraint
  (`attach`/`live`/`detached`).
- `ErrorCategory.TRANSPORT_CONFLICT`, `DEBUG_ATTACH_FAILURE`, `TRANSPORT_FAILURE`,
  `CONFIGURATION_ERROR`.
- The control plane (ADR-0028) already drives every non-terminal `debug_sessions` row of
  a force-crashed System to `detached` (the `live → detached` edge is **owned by #23 and
  must not be re-implemented here**).

So #20 adds: a `connect.py` provider (the realized transport port), `debug.py` (the two
session tools), the app/handler registration, and tests. **No schema migration.**

## Scope

In scope:

- `src/kdive/providers/local_libvirt/connect.py`: a realized `Transport`/`Connector`
  port — `open_transport(system_handle, kind)` opens a single-attach gdbstub by probing
  RSP reachability; `close_transport(handle)` is a best-effort teardown. The RSP framing
  helpers (`rsp_frame`/`valid_rsp_frame`) and the bounded-IO primitives are ported from
  v1 `qemu_gdbstub.py` / `rsp_probe.py` / `bounded.py`. The real socket connect is
  `live_vm`-gated; unit tests inject a fake prober.
- `src/kdive/mcp/tools/debug.py`: `debug.start_session(run_id, transport)` and
  `debug.end_session(session_id)`, plain async handlers wrapped by FastMCP tools.
- Registration: append `debug.register` to `_PLANE_REGISTRARS` in `mcp/app.py`. No job
  handler (both tools are synchronous — the skeleton lists only the five long-running
  provider ops as job kinds; debug session ops are synchronous).
- Tests: `tests/mcp/test_debug_tools.py` (handler-level), `tests/providers/
  local_libvirt/test_connect.py` (provider-level).

Out of scope (other issues): the Debug plane operations and `debug.py`'s breakpoint/
read tools (#22); reattach (M1); the dead-session reconciler sweep (M1.5).

## Behavior

### `debug.start_session(run_id, transport="gdbstub")`

1. Resolve the Run by id (project-scoped; a missing/cross-project Run is a not-found-
   shaped `configuration_error`). RBAC: `operator` on the Run's project.
2. Reject any `transport` other than `"gdbstub"` (`configuration_error`) — M0 ships one.
3. Under the **per-System advisory lock** of the Run's System (`LockScope.SYSTEM`,
   serializing against `force_crash` and teardown), enforce **single-attach**: if any
   `debug_sessions` row joined to **the same System** (through `runs.system_id`) is in
   `attach` or `live`, return `transport_conflict`. (Single-attach is per **gdbstub
   endpoint = per System**, not per Run — two Runs on one System share the one stub.)
4. The System must be `ready` (the only state with a live, attachable guest). A System
   not `ready` is a `configuration_error` carrying `current_status`.
5. Open the transport: `connect.open_transport(system_handle, "gdbstub")`. A stub that
   does not answer RSP framing is `debug_attach_failure`; a transport/socket fault is
   `transport_failure`. On failure **no row is inserted** (the attach is aborted, not
   stranded in `attach`).
6. On success, insert one `debug_sessions` row, then drive it `attach → live` in the
   same transaction, recording the `transport_handle` and an initial
   `worker_heartbeat_at`. Audit `->attach` then `attach->live`. Return the
   `debug_session_id` with `status="live"`.

The insert+transition+audit run inside `conn.transaction()` under the System lock, so a
concurrent `force_crash` either runs entirely before (and this attach then sees the
System `crashed` → `configuration_error`) or entirely after (and detaches the just-
created `live` row). There is no window where a `live` row escapes the lock.

### `debug.end_session(session_id)`

1. Resolve the session (project-scoped; missing/cross-project → `configuration_error`).
   RBAC: `operator`.
2. Under the per-System advisory lock of the session's System, read the row `FOR UPDATE`:
   - Already `detached`: idempotent success (`status="detached"`).
   - `attach` or `live`: close the transport (best-effort), drive `→ detached`, audit
     `{old}->detached`, return `status="detached"`.
3. Closing the transport never raises into the caller: `close_transport` swallows its
   own errors (the row must reach `detached` even if the stub is already gone).

`force_crash`/reboot driving `live → detached` is **#23's** job (ADR-0028
`_detach_sessions`); `end_session` is the agent-initiated detach. Both converge on
`detached` and both hold the per-System lock, so they cannot race to a double transition
(the guard rejects `detached → detached`; the idempotent re-read absorbs the loser).

## The transport port (`connect.py`)

```python
class TransportHandleData(NamedTuple):
    kind: str          # "gdbstub"
    host: str          # loopback IP literal
    port: int          # 1..65535

class Connector(Protocol):
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...
```

`open_transport`:

- Rejects any `kind != "gdbstub"` (`configuration_error`).
- Resolves the System's gdbstub endpoint via an **injected, `live_vm`-gated** resolver
  (the real one reads the libvirt domain's QEMU gdbstub host/port; the default raises
  `MISSING_DEPENDENCY` outside the gate). Unit tests inject a fake resolver + prober.
- Enforces **loopback-only**: the resolved host must be a loopback IP literal — a
  non-loopback or hostname value is `configuration_error` **before any network IO**
  (ported v1 F2: a loopback-local provider must never connect out to a target-supplied
  remote — SSRF defense).
- Probes RSP reachability (one read-only `?` halt-reason exchange, checksum-validated,
  byte-bounded) over the injected prober. A stale/non-RSP listener is rejected as
  `debug_attach_failure` (not accepted as a healthy stub).
- Returns a `TransportHandle` (an opaque serialized `TransportHandleData`) the session
  row stores as `transport_handle`.

`close_transport` is best-effort (the gdbstub is connectionless RSP — there is no
persistent socket to tear down in M0; close is a no-op that swallows any error). The
handle is **not** persisted with secrets — it carries only `kind/host/port` (a loopback
endpoint), which is non-sensitive; no redaction needed, but the handle is built from
provider-resolved values only, never echoed guest output.

## Success criteria (falsifiable)

1. `debug.start_session` on a `ready` System with a reachable stub inserts exactly one
   `debug_sessions` row in state `live` with a non-null `transport_handle` and
   `worker_heartbeat_at`, and returns `status="live"`. (Test: assert the row + envelope.)
2. A second `debug.start_session` for any Run on a System that already has an
   `attach`/`live` session returns `status="error"`, `error_category="transport_conflict"`,
   and inserts **no** new row. (Test: seed a `live` session, attach again, assert one row.)
3. `debug.end_session` on a `live` session drives it to `detached`, audits
   `live->detached`, and returns `status="detached"`. A second `end_session` is an
   idempotent `detached` success with no second audit row.
4. `start_session` on a non-`ready` System returns `configuration_error` with
   `current_status`; on an unreachable stub returns `debug_attach_failure`; on a
   transport fault returns `transport_failure`; on `transport != "gdbstub"` returns
   `configuration_error` — and **no row** in every failure case.
5. `start_session`/`end_session` without `operator` raise `AuthorizationError`.
6. A cross-project or malformed-UUID `run_id`/`session_id` is `configuration_error`
   (not-found-shaped — indistinguishable from missing).
7. `open_transport` rejects a non-loopback resolved host as `configuration_error`
   **without** invoking the prober; rejects a non-`gdbstub` kind as `configuration_error`;
   rejects an RSP-silent listener as `debug_attach_failure`.
8. `mcp/app.py` registers `debug.start_session`/`debug.end_session` (assert via
   `app.list_tools()` in `test_app.py`); no handler is registered (synchronous tools).
9. The full suite is green with **no** `live_vm` host: every real socket/libvirt path is
   a gated seam.

## Failure modes

| Condition | Category | Row effect |
|-----------|----------|------------|
| Run/session missing or cross-project | `configuration_error` | none |
| Malformed UUID | `configuration_error` | none |
| `transport != "gdbstub"` | `configuration_error` | none |
| System not `ready` | `configuration_error` (`current_status`) | none |
| Existing `attach`/`live` session on the System | `transport_conflict` | none |
| Stub does not answer RSP | `debug_attach_failure` | none |
| Resolved host non-loopback | `configuration_error` | none (no IO) |
| Socket/transport fault | `transport_failure` | none |
| `end_session` on already-`detached` | success (`detached`) | none |
| Missing `operator` | raises `AuthorizationError` | none |

## Coordination with #23 (control plane) and #22 (Debug plane)

- **#23 owns** the `force_crash`/reboot `live → detached` transition (`_detach_sessions`,
  joined through `runs`). #20 must **not** add a competing crash→detach path. Both
  `end_session` and `_detach_sessions` hold the per-System lock and converge on
  `detached`; the transition guard + idempotent re-read make the convergence safe.
- **#22 (drgn introspection)** adds its own tool module and edits `app.py` registrars,
  `docs/adr/README.md`, and `tests/mcp/test_app.py` concurrently. #20 keeps its edits to
  those shared files minimal (one registrar append, one README row, the two new
  tool-name assertions) and does not assume #22's modules exist.

## Out-of-scope / deferred

- Reattach (`detached → live`) — M1.
- The dead-session reconciler sweep (a `live` session whose heartbeat lapses → `detached`)
  — M1.5.
- The Debug plane ops (`set_breakpoint`/`read_memory`/`read_registers`) — #22.
- A persistent gdb/MI subprocess owning the RSP socket — M0 opens a transport handle
  (endpoint + reachability proof); the gdb/MI tier lands with #22.
