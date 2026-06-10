# ADR 0086 — Dead-worker gdbstub reconciler reset (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Amends (does not supersede):** [ADR-0079](0079-remote-live-debug-transport.md) (whose
  single-client-contention consequence *named* this reconciler reset as required), and
  [ADR-0083](0083-remote-connect-debug-plane.md) (which shipped the interim
  no-automated-recovery limitation and *deferred* the reset to this follow-up).
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  portability gate this change deliberately extends), [ADR-0021](0021-reconciler-loop-drift-repair.md)
  (the reconciler drift-repair loop + system-attributed GC), [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)
  (the gdbstub Connect plane + DebugSession lifecycle), [ADR-0063](0063-typed-provider-runtime.md)
  (the typed-port provider seam the new port follows), [ADR-0077](0077-qemu-tls-control-transport.md)
  (the `qemu+tls` connection lifecycle the reset reuses).
- **Spec:** [`../superpowers/specs/2026-06-09-dead-worker-gdbstub-reset-design.md`](../superpowers/specs/2026-06-09-dead-worker-gdbstub-reset-design.md)
- **Issue:** #216

## Context

QEMU's gdbstub accepts **one** TCP client. ADR-0079 settled that a stale connection from a
**dead worker** can hold the System's gdbstub and block re-attach, and that the DebugSession
reconciler must **reset the dead-worker transport**, not merely mark the row `detached`, or the
next attach contends with a ghost client — surfacing as `transport_conflict`. ADR-0083 deferred
that reset because it is a change to the provider-agnostic **core** (`src/kdive/reconciler/`)
that the ADR-0076 portability gate blocks — not provider work — and shipped #205 with the
documented interim limitation: between #205 and this follow-up, a worker that dies mid-debug
wedges the System's single-client gdbstub until the System is torn down and reprovisioned.

Two facts pin why the reset must live in core, reached through a port:

1. **`close_transport` cannot fix it.** It is a no-op for connectionless RSP, and the holding
   connection belongs to the **dead** worker — the provider cannot break a connection a live
   worker never owned.
2. **The reconciler must not import a provider.** Freeing the port is provider-specific (it
   re-arms QEMU's gdbstub over the remote host's `qemu+tls` monitor). The reconciler is gated
   core; importing `remote_libvirt` would breach ADR-0076's dependency direction.

The reconciler already reaches provider behavior for leaked-domain repair through a narrow
injected port — `InfraReaper` (`providers/reaping.py`), `NullReaper` default, concrete provider
reaper wired in `composition.py`. This ADR adds the second instance of that pattern.

## Decision

### 1. A new reconciler→provider port: `TransportResetter`

Add `TransportResetter` (a `runtime_checkable` Protocol) and a `NullResetter` default in
`src/kdive/providers/transport_reset.py` — a `providers/` module, **not** a gated core prefix,
exactly like `providers/reaping.py`:

```python
async def reset(self, *, transport: str, transport_handle: str | None,
                domain_name: str | None) -> None
```

The reconciler imports only the Protocol + `NullResetter`. `NullResetter` is the default and
the local-libvirt deployment's resetter: a co-located gdbstub's stale socket is torn down by
the host OS when the worker dies, so local needs no active reset.

### 2. The reconciler routes nothing; the remote resetter self-selects

`_repair_dead_sessions` passes only **core-available** data per detached session — `transport`,
`transport_handle`, `domain_name` — so the reconciler never learns a provider identity.
`RemoteLibvirtTransportResetter` (in `providers/remote_libvirt/`, not gated) no-ops unless the
session is unambiguously its concern: `transport == "gdbstub"`, the handle decodes to a
`gdbstub`-scheme `TransportHandleData`, its host equals the operator-configured
`RemoteLibvirtConfig.gdb_addr`, and `domain_name` is present. A non-matching handle host (a
local loopback gdbstub), a `drgn-live`/`ssh` transport, or a missing handle/domain is a no-op.

### 3. The reset re-arms the gdbstub via the QEMU monitor

The resetter opens a one-shot mutual-TLS connection (`remote_connection`, ADR-0077), looks the
domain up by name, and drives QEMU's gdbstub through libvirt's QEMU-monitor passthrough. The
**required behavior is a contract** — drop the stale client and re-open the single-client stub
on the same port — not an asserted QEMU implementation detail. The candidate sequence is the
explicit stop-then-rearm (`gdbserver none`, then `gdbserver tcp::<port>`), which closes the
holding connection deterministically and avoids an `EADDRINUSE` race against the lingering
socket; the exact sequence and QEMU's response are determined and **verified under the `live_vm`
gate** (the falsifiable check for acceptance). `port` is decoded from the dead session's
`transport_handle`. The `qemuMonitorCommand` call is an injected seam that runs only under
`live_vm`; orchestration, self-selection, handle decoding, the composed command string, and the
error contract are unit-tested with a fake domain/connection — the structure every other remote
seam uses. A libvirt fault maps to `CategorizedError(TRANSPORT_FAILURE)` (an existing category,
ADR-0079).

### 4. Detach first, live-holder guard, reset best-effort

`_repair_dead_sessions` widens its `UPDATE … RETURNING` to carry `transport`,
`transport_handle`, and `run_id`; the detach transaction commits **before** any provider I/O.
Then, per detached row, it resolves the System's `domain_name` and — **before** resetting —
applies a **live-holder guard**: if any `debug_sessions` row for that System is currently `live`
on the `gdbstub` transport, the reset is **skipped and logged**, because a live holder means the
single-client port is legitimately occupied (a new debugger won the freed port between the
detach and now), and re-arming would evict it. Otherwise it awaits `resetter.reset(...)` wrapped
in a logged best-effort guard — a raise is recorded and the sweep continues, exactly like
`repair_leaked_domains`'s per-domain `destroy`. The guard *narrows* the eviction race to the
window between the check and the re-arm rather than holding a per-System lock across network
I/O. The repair's return value stays the detached-row count, so `ReconcileReport.dead_sessions`
is unchanged. `Reconciler.__init__` / `reconcile_once` gain a
`resetter: TransportResetter = NullResetter()` parameter threaded the way `reaper` already is;
`composition.py` gains `build_reconciler_transport_resetter()` (the remote resetter when remote
is enabled, else `NullResetter`); `__main__.py` wires it.

The reset is **scoped to the Topology precondition** (spec): the reconciler is a process
independent of the worker pool, so it survives a worker's death and reaches the QEMU host over
`qemu+tls`. Acceptance is the worker-gone/host-reachable case; a worker process that dies on a
live host already frees the stub via OS `FIN` (the reset is a confirming no-op), and a
partitioned QEMU host fails the reset closed (fallback: today's `transport_conflict`).

### 5. The gate-allowlist extension

The only gated core file this touches is `src/kdive/reconciler/loop.py` (the port, the remote
resetter, `composition.py`, and `__main__.py` are all outside the ADR-0076 core prefixes). Add
`src/kdive/reconciler/loop.py` to `ALLOWED_FILES` in `scripts/m2_portability_gate.py` in this
same PR, with a comment citing this ADR.

## Consequences

- **The acceptance is met without core importing a provider.** A dead worker's stale gdbstub
  is re-armed by the reconciler through the injected port, so the next `debug.start_session`
  attach connects to a free stub instead of failing `transport_conflict`. The reconciler stays
  provider-agnostic; the gate stays meaningful (one named, reviewed allowlist entry).
- **Best-effort, no regression.** Because detach commits before the reset, a transient reset
  failure (host partitioned, domain already gone) is not retried — but the fallback is exactly
  today's behavior (`transport_conflict` on the next attach), so every outcome is at least as
  good as the current always-wedged state. The dead session is always detached regardless.
- **No durable reset-retry.** Retrying a failed reset would need a new `needs_transport_reset`
  signal swept independently of session state; deliberately out of scope for M2 (no speculative
  column). Revisit only if operational data shows transient reset failures are common.
- **`TransportResetter` is the second reconciler provider seam after `InfraReaper`.** A future
  provider that needs an active transport reset (none in M2) wires its own resetter — and a
  `_CompositeResetter` fan-out — through `composition.py`, the way `_CompositeReaper` already
  composes multiple reapers. Not built now (only remote needs a reset).
- **The reset never evicts a legitimate debugger, and a declined reset is observable.** The
  live-holder guard skips the re-arm when a `live` gdbstub session holds the System; a
  self-deselected or skipped reset is logged (reconciler logs the attempt, resetter logs its
  decision + reason), so a port left wedged by a declined reset is visible in logs rather than
  silently skipped.
- **No new error strings.** An unreachable host / domain / monitor → `transport_failure`
  (ADR-0079); the contended-attach the reset prevents is the existing `transport_conflict`.
- **`live_vm` boundary unchanged.** The real `gdbserver` re-arm against QEMU is `live_vm` /
  operator-runbook, like the rest of the remote debug plane; CI proves the orchestration and
  self-selection with fakes.

## Considered & rejected

- **Reset-then-detach (gate the detach on a successful reset).** Rejected: a permanently
  unreachable host (partition, torn-down domain) would pin a dead session `live` forever, the
  opposite of the reconciler's job. A dead session must detach whether or not the port frees.
- **Durable reset-retry via a `needs_transport_reset` column swept independently.** Rejected
  for M2: speculative complexity for a failure mode (transient reset failure) that is no worse
  than today's behavior. Revisit with operational evidence.
- **Reconciler imports `remote_libvirt` directly.** Rejected: breaches ADR-0076's dependency
  direction; the injected-port pattern (`InfraReaper`) is the established, gate-clean seam.
- **Per-System `ResourceKind` routing in the reconciler.** The reconciler could resolve each
  System's provider kind and dispatch. Rejected: it pushes provider-selection logic into core;
  self-selection by the resetter (transport kind + handle host == operator `gdb_addr`) keeps the
  reconciler passing only data it already owns.
- **A TCP-only reset (open a socket to the stub and close it).** Rejected: a single-client stub
  refuses a second connection, and nothing outside QEMU can evict the existing client; freeing
  the port requires the QEMU-monitor re-arm.
- **Unconditional re-arm (reset every detached gdbstub session without a live-holder check).**
  Rejected: the reset runs after the detach commits, so a new debugger can win the freed port in
  the interval; an unconditional re-arm would evict that legitimate live client. The live-holder
  guard skips the re-arm when a `live` gdbstub session exists for the System, turning the fix
  from a possible cause of a wedge back into only a fix.
- **A bare `gdbserver tcp::<port>` re-issue as the re-arm.** Rejected in favor of the explicit
  `gdbserver none` → `gdbserver tcp::<port>` stop-then-rearm: re-issuing onto a port still held
  by the lingering socket risks `EADDRINUSE` and leans on undocumented re-issue semantics,
  whereas the explicit teardown closes the holding connection deterministically first. The exact
  sequence is pinned under `live_vm`.
- **Extend the gate-allowlist for this in #205.** Rejected there and tracked here: it crosses
  the core boundary for a change that is not provider work, so it gets its own ADR + reviewed
  allowlist entry — this one.
