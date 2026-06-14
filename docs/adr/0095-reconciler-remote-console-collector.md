# ADR 0095 — Reconciler-supervised remote console collector (M2.5)

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0076](0076-remote-libvirt-provider-package.md) (the `remote_libvirt`
  package this adds to), [ADR-0062](0062-platform-operations.md) (the `reconcile_once`
  pass-based reconciler and its per-class repair report this extends),
  [ADR-0086](0086-dead-worker-gdbstub-reconciler-reset.md) (the reconciler→provider port
  pattern this follows for a second long-lived remote concern),
  [ADR-0049](0049-crash-capture-tiers.md) (the `CaptureMethod` vocabulary, unchanged).
- **Spec:** [`../superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md`](../archive/superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md) §3.

## Context

`local-libvirt` captures console by teeing the guest's serial console to a worker-local
`<log file>` from boot (`local_libvirt/lifecycle/provisioning.py:164`); the accumulated log is
the console artifact, and it covers boot through crash. `remote-libvirt` cannot tee to a host
path it can't read, so it renders the serial console **without** a `<log>` tee and instead
proves boot readiness via boot_id-change (`remote_libvirt/install.py`).

libvirt's host-side console channel is `virDomainOpenConsole`, which returns a **live stream**
— it delivers only output produced **after** the stream opens, with no replayable backing log.
So console parity (boot → crash) requires a **long-lived owner** that opens the stream at boot
and tees continuously. A worker job cannot be that owner: it pulls one durable job, runs one
provider operation, and returns; holding a console stream open for a System's entire life would
pin a worker slot indefinitely and break the job model.

The only long-lived control component is the **reconciler**. But the reconciler is
**pass-based** (`reconcile_once` runs a discrete repair sweep — provider reaping, image sweeps
— and returns a per-class report); a continuously-running stream does not fit *inside* a pass.

One subtlety this forces: the reconciler's pass runs on an interval (`DEFAULT_INTERVAL` = 30s),
so if the collector were *started* by the pass it would attach up to ~30s after the domain
boots and miss early-boot console — the firmware/early-kernel lines where a boot-time crash
often prints, and unrecoverable because `virDomainOpenConsole` is future-only. Capturing
boot→crash therefore requires the stream to open **promptly at boot**, not on the 30s repair
pass — and because a libvirt stream can't be handed across processes, "promptly" means a
continuous sub-tick attach-watcher in the hosting process, not a hand-off from the boot worker
(see the Decision's attach mechanism).

## Decision

Split the concern into a long-lived streamer **hosted only by the periodic reconciler process**
and a **supervision repair class** driven by that process's `reconcile_once` pass.

- **Streamer** (`remote_libvirt/console_collector.py`, new): a per-System task that opens
  `virDomainOpenConsole`, appends decoded output to a bounded rolling buffer, and reconnects on
  stream drop. Because S3 has no append, the buffer **rotates** on a size threshold by uploading
  a numbered **part object** (`…/console/<n>`). **Every part is redacted before upload** — the
  redactor runs at the rotation boundary, not only at finalize — and to catch a secret straddling
  the rotation seam the redactor **re-scans a trailing overlap** of the previous part's tail with
  the next part's head. (Residual risk of a secret longer than the overlap window remains; the
  goal is to minimize, not to claim zero leak.) **Assembly is kdive-side, not S3 multipart.** S3
  multipart requires ≥5 MiB parts, which would defeat the small rotation threshold the crash-tail
  durability trade wants; instead the parts are small, and finalization reads the ordered parts
  back and writes **one** concatenated console object. The consequence — the single artifact
  `classify_console`/`read_console_log` expect does not exist until finalize; mid-run, only the
  numbered parts are present — is stated, not hidden. Finalization (on capture or teardown)
  flushes+redacts the tail part, assembles the single object in the same shape local produces,
  and registers it, so the boot-plane consumer and artifact search stay provider-agnostic.
- **Prompt attach, decoupled from the 30s pass.** A `virDomainOpenConsole` stream is bound to
  the libvirt connection in the process that opens it, so it **cannot be handed across
  processes** — the worker that runs the boot plane cannot open a stream and pass it to the
  reconciler-leader that hosts it. Attach latency is therefore real, and the way to keep it from
  becoming the ~30s early-boot gap is to **decouple attach from the repair pass**: the leader
  runs a **continuous in-process attach-watcher** that, at sub-tick cadence, opens a stream for
  any running remote System lacking a live collector. The 30s `reconcile_once` supervision class
  does only **liveness/reap** (restart a dead stream, reap a gone System), never the initial
  open. The honest early-boot bound is then the attach-watcher's latency (sub-second), **not** a
  reconcile tick. Only the single-process case — boot and hosting co-located, i.e. local-libvirt,
  not remote — can truly open at domain start; on remote, the sub-second attach gap is the
  accepted limit, and the firmware/early-kernel lines within it are the residual loss.
- **Supervisor** (new `reconcile_once` class): each pass keeps the existing collectors healthy —
  restart one whose stream died, reap collectors for Systems that are gone — and reports the
  transitions per-class like the existing reaper and image sweeps. The **initial open** is the
  continuous attach-watcher's job (above), not the pass's; the pass never opens a first stream.
  The pass is the supervisor; it never streams itself. Per-System collector failures are
  isolated in the report (a stuck collector for one System never blocks the pass), mirroring
  `reconcile_once`'s per-repair isolation. **Reap never races a teardown-finalize:** since
  finalization fires "on capture or teardown" and the reap fires when a System "is gone," the two
  touch the same collector from different actors. The supervisor reaps a collector **only after
  its finalize has assembled and registered the artifact** (a collector with a pending finalize
  is skipped, not reaped), so a teardown's console is never discarded before it is persisted.

**Hosting vs. supervision — the two-process / multi-replica hazard.** `reconcile_once` runs in
**two** processes: the periodic reconciler loop (`reconciler/loop.py`) *and* the `ops.reconcile_now`
MCP tool, which executes in the **server** process (`mcp/tools/ops/reconcile.py`). Collectors are
hosted **only** in the periodic reconciler process, so:
- The collector supervision class runs under a **single predicate: this invocation is the
  periodic reconciler loop *and* holds the hosting leader lock.** Supervision and hosting share
  the one leadership predicate, so the supervisor only ever manages collectors the same
  invocation hosts. That gate excludes both wrong-process and wrong-replica callers: an on-demand
  `ops.reconcile_now` (server process) is not a reconciler loop, and a **non-leader** reconciler
  replica's loop does not hold the lock — so neither starts, supervises, nor reaps collectors,
  and only the leader opens streams.
- Collector hosting requires a **single** active hosting process. The reconciler is otherwise
  safe to replicate (its repairs are `advisory_xact_lock`-guarded), but the hosted streams are
  not lock-guarded, so two replicas would open duplicate console streams per System. Hosting is
  therefore guarded by a **session-scoped** advisory lock leadership claim — **not** the
  transaction-scoped `advisory_xact_lock` the repairs use (which releases at transaction end and
  so cannot hold leadership across streamers that live *between* passes). Leadership is a
  `pg_advisory_lock` held on a dedicated long-lived connection for the reconciler's life; only the
  lock-holder hosts collectors, and a standby replica runs every other repair class but hosts
  none. This is a deployment invariant, not left implicit.
- **Failover cold-starts every console.** When the leader dies and a standby acquires the
  session lock, it must start collectors for every running System from scratch — and because
  `virDomainOpenConsole` is future-only, all pre-failover console history for *every* System is
  lost (a larger loss than the single-System crash-tail window in §Consequences). This is an
  accepted limitation: console is a best-effort diagnostic record, and host_dump/kdump remain the
  durable crash-core path. A standby that takes over re-opens streams promptly to bound the gap.

The reconciler→collector boundary is a new injected port following the `register_with_reaper`
pattern (ADR-0086), so the supervision pass is unit-testable with a fake collector registry and
no libvirt host.

Advertise `CONSOLE` in the remote runtime's `supported_capture_methods`. As on local, console
is a boot-plane artifact consumed off the artifact/`classify_console` path, **not** through
`vmcore.fetch`. One difference from local is deliberate: remote does **not** use the console for
**boot readiness** (it proves readiness via boot_id-change, `remote_libvirt/install.py`), so the
collector's artifact is a crash/diagnostic record, not a readiness signal — the artifact *shape*
matches local, the readiness *consumption* does not.

## Alternatives considered

- **Capture-time panic spew only** (open the stream around `force_crash`, collect the oops):
  much smaller, no new subsystem, but loses boot-phase console — rejected in favor of true
  boot→crash parity with local.
- **Dedicated `console-collector` process:** cleanest separation but adds a fourth process to
  deploy, supervise, and document (compose/helm/runbook) for one capture method — rejected; the
  reconciler already exists and already owns long-lived remote concerns.

## Consequences

- Remote reaches console parity covering boot through crash, over the existing TLS connection.
- The reconciler gains a fourth repair class and its first **continuously-running** hosted task
  (prior classes were stateless sweeps); the supervisor/streamer split keeps the pass itself
  stateless and fast.
- **Durability of the crash tail is the real trade, not generic "recent output."** If the
  reconciler (or its host) dies mid-stream, the unflushed buffer since the last rotation is lost
  — and the highest-value bytes, the panic/oops spew at the very end, sit in exactly that buffer
  at exactly the moment the System (and possibly the host) is under the most stress. Two
  mitigations narrow the window: the collector **flushes a part immediately on detecting a crash
  marker** in the stream (`classify_console`'s crash signature), and rotation uses a **small
  threshold** so the steady-state unflushed window is bounded. Residual crash-tail loss after a
  hard reconciler kill is an accepted M2.5 limitation; the host_dump/kdump capture methods remain
  the durable crash-core path, and a fully durable console journal is a follow-up if needed.
