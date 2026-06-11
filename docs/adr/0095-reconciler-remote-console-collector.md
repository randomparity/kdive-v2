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
- **Spec:** [`../superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md`](../superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md) §3.

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

## Decision

Split the concern into a long-lived streamer **hosted by the reconciler process** and a
**supervision repair class** driven by each `reconcile_once` pass.

- **Streamer** (`remote_libvirt/console_collector.py`, new): a per-System task that opens
  `virDomainOpenConsole`, appends decoded output to a bounded rolling buffer, rotates/uploads on
  a size threshold (S3 has no append), and reconnects on stream drop. Finalization — on capture
  or teardown — flushes, redacts, and registers the **console artifact** in the same shape
  local produces, so `classify_console` and artifact search stay provider-agnostic.
- **Supervisor** (new `reconcile_once` class in `reconciler/loop.py`): each pass ensures a live
  collector exists for every running remote System — start missing, restart dead, reap
  collectors for Systems that are gone — and reports the transitions per-class like the existing
  reaper and image sweeps. The pass is the supervisor; it never streams itself. Per-System
  collector failures are isolated in the report (a stuck collector for one System never blocks
  the pass), mirroring `reconcile_once`'s per-repair isolation.

The reconciler→collector boundary is a new injected port following the `register_with_reaper`
pattern (ADR-0086), so the supervision pass is unit-testable with a fake collector registry and
no libvirt host.

Advertise `CONSOLE` in the remote runtime's `supported_capture_methods`. Console is consumed
off the boot plane (not `vmcore.fetch`), identical to local.

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
- If the reconciler restarts mid-stream, console output between the last rotation-upload and the
  restart is lost. Accepted for M2.5 (boot + most of the run survive); a fully durable console
  journal is a follow-up if needed.
