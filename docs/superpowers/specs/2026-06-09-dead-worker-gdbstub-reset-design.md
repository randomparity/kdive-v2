# Dead-worker gdbstub reconciler reset (issue #216)

- **Status:** Draft
- **Date:** 2026-06-09
- **Issue:** [#216](https://github.com/randomparity/kdive/issues/216)
- **ADR:** [ADR-0086](../../adr/0086-dead-worker-gdbstub-reconciler-reset.md)
- **Milestone:** M2 — Remote libvirt
- **Follow-up to:** #205 (PR #214, ADR-0083); sibling of #215 (ADR-0085)

## Problem

QEMU's gdbstub is **single-client**: it accepts one TCP connection at a time. ADR-0079
(§Consequences, "Single-client gdbstub contention must be reconciled, not just detected")
named the failure mode this spec closes:

> a stale TCP connection from a dead worker can hold it and block re-attach … The
> DebugSession reconciler must **reset the dead-worker transport**, not merely mark the row
> `detached`, or the next attach contends with a ghost — surfacing as `transport_conflict`.

ADR-0083 (§Consequences) recorded the interim limitation #205 shipped with:

> a worker that dies mid-debug can leave its stale TCP connection holding the System's
> single-client gdbstub, and the next attach fails (`transport_conflict` /
> `debug_attach_failure`) until the System is torn down and reprovisioned. `close_transport`
> is a no-op (connectionless RSP) and the holding connection belongs to the dead worker, so
> the provider cannot break it without the core reconciler reset.

Today `reconciler/loop.py::_repair_dead_sessions` flips a stale `live` DebugSession to
`detached` and stops there. The gdbstub port stays held by the dead worker's lingering
connection. The next `debug.start_session` attach to that System fails with
`transport_conflict` until the System is torn down — a developer-visible wedge with no
automated recovery.

## Why this is a core change, not provider work

The reconciler (`src/kdive/reconciler/`) is provider-agnostic core behind the ADR-0076
portability gate. Freeing the gdbstub port is provider-specific (it re-arms QEMU's gdbstub
over the remote host's `qemu+tls` monitor), so the reconciler cannot do it directly without
importing a provider — which would breach the gate's dependency direction.

The codebase already solves this exact shape for leaked-domain repair: the reconciler
consumes a narrow injected **port** (`InfraReaper` Protocol, `providers/reaping.py`) with a
`NullReaper` default, and `providers/composition.py` wires the concrete provider reaper. This
spec adds the second instance of that pattern. Because the reconciler's consumption of the
new port lives in `reconciler/loop.py` (a gated core file), the change carries its own ADR
(ADR-0086) and an explicit `scripts/m2_portability_gate.py` `ALLOWED_FILES` extension in the
same PR — exactly as the issue requires.

## Topology precondition (what makes the reset effective)

The reset recovers a bounded slice of the failure space, and the spec scopes its acceptance to
it rather than over-claiming. The reconciler is a **separate process** (`python -m kdive
reconciler`), not co-located with the worker pool. The reset is effective only when:

- the reconciler is **independent of the dead worker** (it survives the worker's death), and
- the QEMU host is **reachable from the reconciler over `qemu+tls`** even though the worker is
  gone.

This is the common remote topology (worker pool, reconciler, and QEMU host are distinct
machines). Two cases sit outside it: when a worker *process* dies but its host is up, the
worker host's OS already sends `FIN` to the remote stub and the port frees itself — the reset
is a confirming no-op; when the *QEMU host itself* is partitioned, the reconciler cannot reach
its monitor to re-arm and the reset fails closed (the session is still detached; fallback is
today's `transport_conflict`-on-next-attach). The reset's target is the in-between case: a
worker gone (host down or hard-killed) while the QEMU host stays reachable from an independent
reconciler.

## Design

### 1. `TransportResetter` — the new reconciler→provider port

A narrow Protocol under `src/kdive/providers/transport_reset.py` (a `providers/` module, **not**
a gated core prefix), mirroring `providers/reaping.py`:

```python
@runtime_checkable
class TransportResetter(Protocol):
    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None: ...

class NullResetter:
    async def reset(self, *, transport, transport_handle, domain_name) -> None:
        return None
```

The reconciler imports only this Protocol + `NullResetter`. The default is `NullResetter`
(local-libvirt needs no active reset — its gdbstub is co-located, so a dead worker's socket is
torn down by the host OS; the contention ADR-0079 reconciles is the *remote* half-open-TCP
case).

### 2. The remote resetter self-selects; the reconciler routes nothing

`RemoteLibvirtTransportResetter` (under `providers/remote_libvirt/`, not gated) realizes the
port. The reconciler passes only **core-available** data per dead session — `transport`,
`transport_handle`, `domain_name` — and the resetter decides whether the session is its
concern, so the reconciler never learns a provider identity:

- `transport != "gdbstub"` → no-op (`drgn-live` is connectionless, ADR-0083 §4; `ssh` carries
  no gdbstub).
- `transport_handle is None`, or it decodes to a non-`gdbstub` scheme → no-op.
- the decoded handle host **≠** the operator-configured `RemoteLibvirtConfig.gdb_addr` → no-op
  (a local-libvirt loopback gdbstub session is not the remote resetter's to touch).
- `domain_name is None` → no-op (the monitor re-arm needs the domain to look up).
- otherwise: re-arm the gdbstub (below).

A single resetter (not a composite) covers M2: only remote needs an active reset. A second
provider that needs one later adds a composite the way `_CompositeReaper` fans out — deferred
until a second provider exists (no premature abstraction).

### 3. The reset re-arms the gdbstub via the QEMU monitor

`reset` opens a one-shot mutual-TLS connection with the existing `remote_connection`
lifecycle (ADR-0077), looks the domain up by name, and drives QEMU's gdbstub through libvirt's
QEMU-monitor passthrough (`qemuMonitorCommand`, HMP flag). `port` comes from decoding the dead
session's `transport_handle` (`TransportHandleData`); `gdb_addr` is operator config.

**The required behavior is a contract, not an asserted QEMU implementation detail:** the reset
must drop the stale client and re-open the stub so the next attach connects to a free single-
client port. The candidate command sequence is the **explicit stop-then-rearm** —
`gdbserver none` (disable the stub, dropping any connected client) followed by `gdbserver
tcp::<port>` (re-open on the same port) — rather than relying on the re-issue semantics of a
bare `gdbserver tcp::<port>`, because the explicit teardown closes the holding connection
deterministically and side-steps an `EADDRINUSE` race against the lingering socket. The exact
sequence and QEMU's response are **determined and verified under the `live_vm` gate / operator
runbook** — that live step is the falsifiable check for "the port is actually freed" (acceptance
#1). This mirrors every other remote seam: the slow host interaction (`qemuMonitorCommand`) is
an injected seam that runs only under `live_vm`; the orchestration, self-selection, handle
decoding, the composed command string, and the error contract are unit-tested with a fake
domain/connection. A libvirt error maps to `CategorizedError(TRANSPORT_FAILURE)` (an existing
category, ADR-0079 — no new strings).

### 4. The reconciler change (`reconciler/loop.py`, the one gated file)

`_repair_dead_sessions(conn, stale_after, resetter)`:

1. The existing bulk `UPDATE … SET state='detached' … RETURNING` is widened to return
   `id, transport, transport_handle, run_id`. The detach transaction commits first.
2. **After** the transaction commits (never holding a DB transaction open across provider
   network I/O), for each detached row: resolve `domain_name`
   (`SELECT s.domain_name FROM runs r JOIN systems s ON s.id = r.system_id WHERE r.id = %s`).
3. **Live-holder guard (do not evict a legitimate re-attach).** Before resetting, re-check that
   **no** `debug_sessions` row for that System is currently `live` on the `gdbstub` transport
   (`SELECT 1 FROM debug_sessions ds JOIN runs r ON r.id = ds.run_id WHERE r.system_id = %s AND
   ds.state = 'live' AND ds.transport = 'gdbstub'`). If one exists, **skip the reset** and log
   it — a live holder means the single-client port is legitimately occupied (a new debugger won
   the freed port between our detach and now), not wedged; re-arming would kick it. This
   *narrows* the race to the tiny window between this check and the re-arm rather than holding a
   per-System lock across the network reset; the residual worst case is one spurious eviction
   that the evicted debugger re-attaches through — no worse than today's transient. The reset is
   skipped, not failed.
4. Otherwise `await resetter.reset(...)`, wrapped best-effort — a raise is logged and the sweep
   continues, exactly like `repair_leaked_domains`'s per-domain `destroy`. The reconciler logs
   the attempt; the resetter logs its decision (re-armed vs. self-deselected, with the reason),
   so a still-wedged port is never silently skipped.
5. The return value stays the detached-row count, so `ReconcileReport.dead_sessions` keeps its
   meaning.

`Reconciler.__init__` / `reconcile_once` gain a `resetter: TransportResetter = NullResetter()`
parameter threaded into `_repair_dead_sessions`, mirroring how `reaper` is threaded.
`composition.py` gains `build_reconciler_transport_resetter()` (remote resetter when remote is
enabled via `is_remote_libvirt_configured()`, else `NullResetter`), and `__main__.py` passes it
to `Reconciler`.

### Ordering: detach first, reset best-effort

Detach is the durable repair — a dead session **must** leave `live` whether or not the port
can be freed (its worker is gone). Freeing the port is a best-effort side effect. Because the
detach commits before the reset is attempted, a transient reset failure is **not** retried;
the session is already `detached` and will not re-surface. The fallback on a failed reset is
exactly today's behavior — the next attach contends and surfaces `transport_conflict` — so
this is a strict no-regression improvement over the current always-wedged state. Durable
reset-retry (a `needs_transport_reset` flag swept independently of session state) is out of
scope; revisit only if operational data shows transient reset failures are common.

## Acceptance

1. **After a gdbstub-holding worker dies — in the Topology precondition's case (worker gone,
   QEMU host reachable from an independent reconciler) — the reconciler frees the port so the
   next attach succeeds instead of `transport_conflict`.** Unit-covered at two boundaries: the
   reconciler detaches the stale `live` session **and** invokes `resetter.reset` with its
   `gdbstub`/handle/`domain_name`; the remote resetter composes the stop-then-rearm command
   sequence against the domain. The end-to-end re-attach against a real QEMU stub — the
   falsifiable "port is actually freed" check — is `live_vm` / operator-runbook territory (same
   boundary as the rest of the remote debug plane).
2. **A test covers the dead-worker-session → reset → re-attach path** (the reconciler test
   above), plus: the **live-holder guard** — a System with a fresh `live` gdbstub session is
   **not** reset (no eviction); and negative coverage — a NULL-heartbeat or non-stale session is
   neither detached nor reset; a non-`gdbstub` transport and a non-matching handle host are
   no-ops in the resetter.

## Edge cases and failure modes

- **NULL heartbeat** — never swept (a just-attached session that has not beaten yet);
  unchanged from today.
- **Non-stale heartbeat** — not a candidate; no detach, no reset.
- **drgn-live / ssh transport** — detached as before; the resetter no-ops (connectionless /
  no gdbstub).
- **local-libvirt gdbstub session** — detached as before; the remote resetter no-ops (handle
  host is loopback, not `gdb_addr`); the default deployment wires `NullResetter` anyway.
- **A new debugger re-attached in the detach→reset window** — the live-holder guard (§4 step
  3) finds a `live` gdbstub session for the System and skips the reset, so the reconciler does
  not evict a legitimate re-attach. Residual: the sub-second window between the guard check and
  the re-arm; worst case is one spurious eviction the debugger re-attaches through.
- **Domain already gone (System torn down)** — the monitor look-up fails; caught, logged,
  swept onward — the port is moot once the domain is gone.
- **Host unreachable (network partition)** — `qemu+tls` connect fails; caught, logged; the
  session is still detached. Falls back to today's behavior (outside the Topology precondition).
- **Self-deselected reset (NULL `domain_name`, handle host ≠ `gdb_addr`, non-gdbstub
  transport)** — the resetter logs the decision + reason; the reconciler logs the attempt, so a
  port left wedged by a declined reset is visible in logs, not silent.
- **Reset raises** — logged at warning with the session id and the `transport_conflict`
  fallback named; never starves the other repairs or the rest of the dead-session sweep.

## Out of scope

- Durable reset-retry / a `needs_transport_reset` column.
- A composite multi-provider resetter (only remote needs a reset in M2).
- Local-libvirt active reset (co-located; OS frees the socket on worker death).
- Bare-metal KGDB-over-SoL reset (a later provider swaps the transport entirely).
