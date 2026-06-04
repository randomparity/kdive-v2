# ADR 0032 — Connect plane (gdbstub) + DebugSession lifecycle (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #20 (M0: Connect plane (gdbstub) + DebugSession lifecycle)
- **Depends on:** [ADR-0028](0028-control-plane-power-force-crash.md) (the `force_crash`
  `live → detached` edge this plane must **not** duplicate, the `debug_sessions`
  join-through-`runs` pattern, and the per-System advisory-lock discipline),
  [ADR-0025](0025-provisioning-plane-libvirt.md) (the `ready` System and its libvirt
  domain the gdbstub attaches to, and the seam-injected `live_vm`-gated provider shape),
  [ADR-0026](0026-investigation-run-lifecycle.md) (the Run whose System the session binds
  to, and the `run.system → allocation` binding invariant),
  [ADR-0019](0019-tool-response-envelope.md) (the response envelope),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (the `operator` role + audit record),
  [ADR-0009](0009-capability-provider-dispatch.md) (the `ConnectPlane` capability
  placeholder this plane realizes).
- **Refines:** the M0 Connect/Debug wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) (the
  `debug.start_session(run_id, "gdbstub")` / `debug.end_session(session_id)` surface, the
  single-attach `transport_conflict` rule, and the DebugSession `attach → live → detached`
  lifecycle).
- **Spec:** [`../superpowers/specs/2026-06-04-connect-plane-gdbstub-design.md`](../superpowers/specs/2026-06-04-connect-plane-gdbstub-design.md)

## Context

A booted, `ready` System runs a guest behind a QEMU gdbstub. Issue #20 adds the
**Connect plane**: open a single-attach transport to that stub and own the durable
`DebugSession` row across the attachment, via two synchronous tools —
`debug.start_session(run_id, "gdbstub")` and `debug.end_session(session_id)`.

Nearly all of the durable machinery already exists on `main`: the `DebugSession` model
and `DEBUG_SESSIONS` repository, the `DebugSessionState` lifecycle (`attach → live →
detached`, plus `attach → detached`) and its guard, the `debug_sessions` table +
`debug_sessions_state_check` constraint, and the `TRANSPORT_CONFLICT` /
`DEBUG_ATTACH_FAILURE` / `TRANSPORT_FAILURE` categories. The control plane (ADR-0028)
already drives every non-terminal `debug_sessions` row of a force-crashed System to
`detached`. So #20 adds **no schema migration** — only the realized transport port
(`connect.py`), the two tools (`debug.py`), the registration wiring, and tests. The
decisions the parent spec leaves open are settled here.

## Decision

### 1. Single-attach is enforced **per System (per gdbstub endpoint)**, not per Run

The skeleton: "QEMU `gdbstub` transport (single-attach — a second attach is
`transport_conflict`)". One System has one running guest behind one gdbstub. Two Runs can
target the same System (a new Run is the recovery path on a System); they share the one
stub. So the conflict check is: does **any** `debug_sessions` row joined through
`runs.system_id` to the attaching Run's System sit in `attach` or `live`? If so,
`transport_conflict`. Keying the check on the Run alone would let two Runs on one System
both attach to the same stub — exactly the double-attach the rule forbids. This reuses
ADR-0028's `debug_sessions ⋈ runs(system_id)` join.

### 2. `start_session` requires a Run that **booted** — `SUCCEEDED` + a succeeded `boot` step

A DebugSession is the "one boot = one session" attach (skeleton), so it must bind to a Run
whose guest actually booted. But the Run *state* is not that signal: after `runs.build` the
build handler drives the Run `running → SUCCEEDED` (terminal), and `runs.install` / `runs.boot`
are step-ledger ops that leave the Run state at `SUCCEEDED` untouched (`runs.py`). So the
boot signal is a **succeeded `boot` `run_steps` row**, exactly as `runs.boot` itself gates on
a succeeded `install` step. `start_session` therefore requires `run.state is SUCCEEDED`
**and** a succeeded `boot` step; a Run that built but never booted is a `configuration_error`
(`reason="boot_first"`), a non-`succeeded` Run is a `configuration_error` (`current_status`).
This guard is a plain read run before the System lock. Without it, a session could be minted
against a Run that has no live boot behind the stub.

### 3. The session tools are **synchronous** — no JobKind, no handler

The skeleton's M0 job kinds are the five long-running provider ops (`provision`, `build`,
`install`, `boot`, `capture_vmcore`); "everything else (breakpoints, reads, power state)
is synchronous". Opening a gdbstub transport is a bounded RSP probe (sub-second), not a
minutes-long op. So `debug.start_session`/`end_session` are plain async tool handlers that
do their work inline under the per-System advisory lock and return a terminal envelope —
**no `JobKind`, no `_HANDLER_REGISTRARS` entry, no `jobs_kind_check` change**. `app.py`
gains exactly one `_PLANE_REGISTRARS` append.

### 4. A realized `Connector` port, seam-injected and `live_vm`-gated, mirroring the other planes

The slow/host-bound steps — resolving the System's gdbstub host:port from its libvirt
domain and the real socket connect — are **injected seams** defaulting to real
implementations guarded behind the `live_vm` gate (the default resolver raises
`MISSING_DEPENDENCY` outside the gate; the prober's real socket path is `live_vm`-only).
So the open/probe orchestration and the full error contract are unit-tested with fakes;
the real host path runs only under `live_vm`. This mirrors `LocalLibvirtControl` /
`LocalLibvirtRetrieve`.

```python
class TransportHandleData(NamedTuple): kind: str; host: str; port: int
class Connector(Protocol):
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...
```

### 5. Loopback-only, enforced **before any network IO** (ported v1 F2 / SSRF defense)

The resolved gdbstub host must be a **loopback IP literal**. A non-loopback IP or a
hostname is a `configuration_error` raised *before* the prober runs — a loopback-local
provider must never initiate an outbound RSP connect to a target-supplied remote host
(an SSRF-like escalation from guest/domain metadata). This is the v1 `qemu_gdbstub.py`
"F2" control, ported verbatim in intent: reject without DNS or IO.

### 6. RSP-framing reachability, not a bare TCP connect, decides attach success

Attach success requires a complete, checksum-valid `$...#xx` RSP frame in reply to one
read-only `?` halt-reason query (the ported `rsp_probe` exchange, byte-bounded against a
hostile peer). A plain TCP listener that accepts but never answers RSP — or answers
garbage — is rejected as `debug_attach_failure`, **not** accepted as a healthy stub. A
socket/connect fault (refused, timed out, reset) is `transport_failure`. The `?` query is
side-effect-free, so the probe never perturbs guest state.

### 6a. The RSP probe runs **outside** the per-System lock; conflict + ready are re-checked **inside** it

A `pg_advisory_xact_lock` is held until the transaction commits, and the RSP probe is a
bounded-but-multi-second network call. Holding the per-System lock across the probe would
serialize every other System op (`force_crash`'s `_detach_sessions`, teardown) behind a
possibly-dead stub and pin a pool connection across network IO — violating the codebase
convention that the per-System xact lock guards only short DB-bound critical sections
(provider IO lives in job handlers). So `start_session`: (1) does a lockless pre-read to
fast-fail an obviously-doomed attach (System not `ready`, or an existing session), (2) opens
the transport with **no lock or transaction held**, then (3) takes the per-System lock and
**re-checks single-attach and System-ready authoritatively** before inserting. If the locked
re-check fails (the System crashed, or another attach committed first, between the probe and
the lock), it **closes the just-opened transport** and returns the categorized error — no
`live` row escapes the lock, no transport leaks. In M0 `close_transport` is a no-op (the
gdbstub is connectionless RSP), so the lost-race cleanup cost is nil; the close call is the
structurally correct handle-contract cleanup.

### 7. On any attach failure, **no row is inserted**; the `attach` state is never persisted standalone

The DebugSession lifecycle permits `attach → detached` precisely so a failed attach need
not strand a row in `attach`. M0 goes further: `start_session` opens the transport
**first**, and only on success inserts the row and drives it `attach → live` in one
transaction. A failed attach returns the categorized error with **no** `debug_sessions`
row written at all — there is nothing for the (M1.5) dead-session reconciler to sweep, and
the `attach` enum value exists only as the transient pre-`live` state inside the successful
insert transaction. The insert + `attach → live` + audits run under `conn.transaction()`
inside the per-System advisory lock, so no `live` row escapes the lock and a concurrent
`force_crash` is fully serialized either side of it.

### 8. `end_session` is the agent-initiated detach; `force_crash`/reboot detach stays **#23's**

`end_session` reads the session row `FOR UPDATE` under the per-System lock: an
already-`detached` row is an idempotent `detached` success (no second transition, no second
audit); an `attach`/`live` row is closed (best-effort) and driven `→ detached` with a
`{old}->detached` audit. The crash/reboot `live → detached` path is **owned by ADR-0028's
`_detach_sessions`** and is *not* re-implemented here. Both paths hold the per-System lock
and converge on `detached`; the transition guard rejects `detached → detached` and the
idempotent re-read absorbs whichever path loses the race — so a double transition cannot
occur.

### 9. The transport handle carries only `kind/host/port` — a loopback endpoint, provider-resolved, non-sensitive

`transport_handle` is a serialized `TransportHandleData` (`kind`, loopback host, port).
It is built **only** from provider-resolved values — never from echoed guest output or
secrets — so it is safe to persist and to surface in the envelope's `refs`/`data` without
redaction. (Guest-derived data appears only in the #22 Debug-plane *reads*, which redact
before returning; #20 returns no guest bytes.)

## Consequences

- The Connect plane is fully unit-testable with a fake resolver + fake prober; the real
  host path is `live_vm`-gated, so CI stays green with no libvirt/KVM host.
- Single-attach is a per-System invariant enforced under the per-System advisory lock,
  reusing ADR-0028's `debug_sessions ⋈ runs` join — two Runs on one System cannot both
  attach.
- A failed attach leaves **no** durable row; only a successful attach produces a `live`
  `debug_sessions` row. `attach` is a transient in-transaction state, never a stranded
  durable one.
- `end_session` and `force_crash`-detach are race-safe by the shared per-System lock +
  transition guard + idempotent re-read; #20 adds no competing crash→detach path.
- `mcp/app.py` gains one `_PLANE_REGISTRARS` append and **no** handler registrar (the
  tools are synchronous). `tests/mcp/test_app.py` gains two tool-name assertions. These
  three shared files (plus `docs/adr/README.md`) are also touched by #22; #20's edits to
  them are minimal and additive.

## Considered & rejected

- **Key single-attach on `run_id` instead of the System.** Rejected: two Runs can target
  one System (recovery is a new Run), and they share the one gdbstub. A per-Run check
  would permit a double-attach to the same stub — the exact condition `transport_conflict`
  exists to forbid. Single-attach is per gdbstub endpoint = per System.
- **Make `start_session` a job (`JobKind.DEBUG_ATTACH`) with a worker handler.** Rejected:
  the attach is a bounded sub-second RSP probe, not a long-running provider op; the
  skeleton classifies debug ops as synchronous. A job would add a schema constraint value,
  a handler registrar, and an admission/poll round-trip for no latency benefit.
- **Insert the `debug_sessions` row in `attach` *before* opening the transport, then
  advance or fail.** Rejected: a failed attach would strand a durable `attach` row that
  only the (not-yet-built) M1.5 reconciler could clean up. Opening the transport first and
  inserting only on success means a failed attach leaves no row — the `attach → detached`
  edge is reserved for a future reattach/abort path, not used to paper over a failed M0
  attach.
- **Re-implement the crash/reboot `live → detached` transition in the Connect plane.**
  Rejected: ADR-0028 already owns `_detach_sessions` (every non-terminal session of a
  force-crashed System → `detached`, joined through `runs`). Duplicating it would create
  two writers of the same edge and risk a double transition; #20 adds only the
  agent-initiated `end_session` detach, which shares the per-System lock with #23's path.
- **Open the transport inside the per-System lock (single locked critical section).**
  Rejected: the RSP probe is a multi-second network call; holding the per-System xact lock
  across it serializes `force_crash`/teardown behind a possibly-dead stub and pins a pool
  connection across network IO — against the codebase convention that the lock guards only
  short DB-bound sections. The probe runs lock-free; conflict + ready are re-checked
  authoritatively under the lock, which closes the transport and bails on a lost race
  (decision 6a).
- **Skip RSP framing — accept any successful TCP connect as attached.** Rejected: a stale
  or non-gdbstub listener on the port would be mistaken for a healthy stub, and the first
  real Debug-plane op would then fail confusingly. The ported `rsp_probe` exchange proves
  the peer speaks RSP before the attach is reported successful (v1 Decision 5).
- **Allow a caller-supplied or non-loopback gdbstub host.** Rejected: a loopback-local
  provider must not connect out to a target-metadata-supplied remote (SSRF-like). The host
  is provider-resolved and must be a loopback IP literal, validated before any IO (ported
  v1 F2).
- **Open a persistent gdb/MI subprocess that owns the RSP socket at `start_session`.**
  Rejected: that is the Debug-plane (#22) gdb/MI tier. M0's Connect plane returns a
  transport handle (endpoint + a reachability proof); the long-lived MI session lands with
  the breakpoint/read tools, keyed on this session's `transport_handle`.
- **Persist the transport handle through the `Redactor`.** Rejected: the handle carries
  only provider-resolved `kind/host/port` (a loopback endpoint), never guest output or
  secrets, so there is nothing to redact. Guest bytes appear only in #22's reads, which
  redact at their own boundary.
- **Add a schema migration (e.g. a `debug_attach` JobKind value or a session unique
  index).** Rejected: the tools are synchronous (no JobKind) and single-attach is enforced
  by the lock-guarded join, not a partial unique index — keeping #20 migration-free and off
  the shared `0001_init.sql` a sibling issue may edit.
