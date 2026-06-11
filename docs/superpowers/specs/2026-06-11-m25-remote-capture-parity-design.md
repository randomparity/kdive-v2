# M2.5 — Remote-libvirt capture-method parity

- **Status:** accepted (design)
- **Date:** 2026-06-11
- **Deciders:** kdive maintainers
- **Milestone:** M2.5 (GitHub milestone #12) — remote-libvirt capture-method parity
- **ADRs:** [ADR-0094](../../adr/0094-remote-host-dump-via-coredump-volume.md) (host_dump on
  remote via libvirt core-dump-to-volume + stream download — supersedes ADR-0084's
  "host_dump is host-coupled / unsupported on remote" stance),
  [ADR-0095](../../adr/0095-reconciler-remote-console-collector.md) (reconciler-supervised
  remote console collector)
- **Builds on:** [ADR-0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md)
  (the remote Control + Retrieve planes and the two-phase kdump capture this reaches parity
  past), [ADR-0076](../../adr/0076-remote-libvirt-provider-package.md) (the independent
  `remote_libvirt` package + portability diff gate every change here stays inside),
  [ADR-0049](../../adr/0049-crash-capture-tiers.md) (the provider-agnostic `CaptureMethod`
  vocabulary, **unchanged** by this milestone)

## Context

After M2, `remote-libvirt` advertises exactly one crash-capture method —
`supported_capture_methods = {KDUMP}` (`providers/composition.py:260`) — while
`local-libvirt` advertises three: `{CONSOLE, HOST_DUMP, GDBSTUB}` (plus kdump through the
shared Retrieve plane). `#198` proposed deprecating local-libvirt after M2; investigation
(see the `m25-remote-capture-parity` design note) found that premise false. The two
providers are **near-complementary on capture, not redundant**, and local-libvirt is the only
zero-hardware default/CI/reference provider. The real gap is that remote cannot yet capture a
crash three of the four ways local can.

The structural reason is host topology. `local-libvirt` shares a filesystem with its
hypervisor: it tees the serial console to a worker-local `<log file>`
(`local_libvirt/lifecycle/provisioning.py:164`) and `virsh dump`s to a worker-local path, then
reads both directly. `remote-libvirt` (ADR-0076) **shares no filesystem with its host** and
**has no host shell** — `RemoteLibvirtConfig` carries a `qemu+tls://` URI, mutual-TLS cert
refs, and a `storage_pool`, but no ssh/host-agent seam. That is precisely why kdump went
*in-guest* (qemu-guest-agent + presigned PUT, ADR-0084) and why remote provisioning renders
the serial console **without** a `<log>` tee ("the path would be on the remote host",
`remote_libvirt/provisioning.py:116`).

So console and host_dump — both **host-side** artifacts — cannot use local's mechanism. The
enabling observation is that libvirt's TLS API already offers host-side retrieval **without a
shell**:

- `virDomainOpenConsole` returns a **stream** the worker/reconciler reads over the existing
  TLS connection.
- `virDomainCoreDumpWithFormat` writes a core to a host path; targeted into a
  **storage-pool volume**, `virStorageVolDownload` streams it back over the same connection.
  The provider already drives `storage_pool` via `storageVolLookupByName` for overlay disks
  (`remote_libvirt/provisioning.py`).

M2.5 realizes console and host_dump over these stream seams, advertises the already-wired
gdbstub transport as a capture method, and exercises all four against the live remote spine.

## Non-goals

- **No `CaptureMethod` vocabulary or MCP-seam change** (ADR-0049 is untouched). The enum,
  `vmcore.fetch` admission set `_VMCORE_METHODS = {HOST_DUMP, KDUMP}`, and the tool surface
  stay byte-identical.
- **No new host access.** Everything rides the existing libvirt-TLS connection + storage pool.
  No ssh-to-host, host agent, or new credential is introduced.
- **No local-libvirt deprecation.** `#198` stays open; M2.5's exit reframes it as the narrower
  "production default vs. opt-in dev/CI/reference provider" disposition. Local is never deleted.
- **No >5 GiB cores.** host_dump inherits kdump's single-PUT 5 GiB ceiling (ADR-0048);
  multipart is an explicit follow-up, not M2.5 scope.

## Decision

Realize the three missing methods on remote, each over the libvirt-TLS API, then prove all
four live.

### Capture-method matrix

| Method | Status | Mechanism | Byte path |
|--------|--------|-----------|-----------|
| kdump | done (ADR-0084) | in-guest helper, presigned PUT | guest → object store |
| **host_dump** | M2.5 | `virDomainCoreDumpWithFormat` → storage-pool volume → `virStorageVolDownload` | host → worker (redact dmesg, upload) |
| **gdbstub** | M2.5 | already wired (connect plane / RSP) — advertise + confirm selection | live transport, no byte capture |
| **console** | M2.5 | reconciler-supervised `virDomainOpenConsole` collector | host stream → reconciler buffer → artifact |

`host_dump` and `kdump` both satisfy `vmcore.fetch` (both in `_VMCORE_METHODS`); advertising
`HOST_DUMP` in the remote runtime's `supported_capture_methods` is what admits it through the
existing tool. `console` and `gdbstub` are advertised but consumed off the boot/connect planes
— identical to how local surfaces them.

### 1. host_dump on remote (ADR-0094)

A new Retrieve-plane path in `remote_libvirt/retrieve.py`, parallel to the kdump `capture()`:

1. `virDomainCoreDumpWithFormat(path, format, flags)` with `flags = VIR_DUMP_MEMORY_ONLY`
   dumps the live (or NMI-crashed) guest's memory to a path **inside the `storage_pool`
   directory**, named deterministically per System. A stale same-named volume is deleted first.
2. `pool.refresh()` so libvirt discovers the file as a managed volume, then
   `storageVolLookupByName` + `virStorageVolDownload` streams it back over the TLS connection.
3. The worker computes sha256/size, enforces the 5 GiB ceiling, extracts the kernel build-id
   from the core's VMCOREINFO note (`CaptureOutput.vmcore_build_id` is mandatory), extracts +
   redacts dmesg (reusing the shared redaction path), and uploads the core to the object store
   directly (the worker holds the bytes, so no presigned-PUT round trip — unlike kdump).
4. The host volume is deleted in a `finally` (graceful path); volumes orphaned by a
   non-graceful worker/host crash that bypasses `finally` are reaped by a reconciler sweep.

**Dump format:** compressed kdump (`VIR_DUMP_MEMORY_ONLY` +
`VIR_DOMAIN_CORE_DUMP_FORMAT_KDUMP_ZLIB`) is the default — drgn reads makedumpfile-compressed
kdumps natively, and compression keeps cores comparable to the in-guest kdump path. The
memory-only flag is mandatory: without it `format` is ignored and libvirt emits an unreadable
QEMU save image. Uncompressed `RAW` ELF stays available behind a config knob but is **not** the
default — its size ≈ full guest RAM would breach the 5 GiB ceiling on every ordinarily-sized
guest (see ADR-0094).

This **supersedes ADR-0084's** assertion that host_dump is host-coupled and excluded from
remote: a storage-pool volume + stream download is the host-side channel ADR-0084 lacked.

### 2. gdbstub advertisement (no ADR)

The remote gdbstub transport is already wired and exercised live (ADR-0083/0085, the
`remote-libvirt-live-exercise` run). M2.5 adds `GDBSTUB` to the remote runtime's
`supported_capture_methods` and confirms the connect-plane selection path advertises it
identically to local. No architectural change → no ADR. The work is the advertisement, a
selection-confirmation test, and a line in the capstone exercise.

### 3. Reconciler-owned console collector (ADR-0095)

`virDomainOpenConsole` delivers only **future** output (no replayable backing log), so console
parity — boot-through-crash capture — requires a **long-lived** owner that opens the stream at
boot and tees continuously. A worker job cannot host this (it runs one provider operation and
returns; holding a stream open for the System's life would pin a worker slot). The owner is the
**reconciler process**, supervised by a new `reconcile_once` repair class:

- **Streaming** runs as a long-lived per-System task in the reconciler process: open
  `virDomainOpenConsole`, append decoded output to a bounded rolling buffer, reconnect on stream
  drop. On a size threshold the buffer rotates by uploading a **numbered, redacted part object**
  (S3 has no append; redaction runs at the rotation boundary, not only at finalize, so mid-stream
  parts never carry raw console secrets). A crash marker in the stream forces an immediate flush
  so the panic tail is the least-lost part.
- **Attach + supervision:** the single leader-locked reconciler runs a **continuous attach-watcher**
  that promptly (sub-tick) opens a stream for any running remote System lacking a live collector —
  decoupled from the 30s repair pass so early-boot console isn't gapped by a reconcile interval.
  The new `reconcile_once` class does only **liveness/reap** (restart a dead stream, reap a gone
  System), reported per-class like the existing reaper/image sweeps; it never streams itself.
  Because `reconcile_once` also runs on-demand in the **server** (`ops.reconcile_now`), the class
  and the attach-watcher share one predicate — periodic reconciler loop **and** hosting leader —
  so neither the server nor a non-leader replica opens duplicate streams (see ADR-0095).
- **Finalization:** on capture or teardown the tail part is flushed+redacted and the numbered
  parts are assembled into the **single** console artifact `classify_console`/`read_console_log`
  expect — the same shape local produces from its `<log>` tee, so downstream consumers
  (`classify_console`, artifact search) are provider-agnostic.

This adds a fourth reconcile-pass class alongside provider reaping and image sweeps, and a new
reconciler→provider seam (a console-collector port) following the `register_with_reaper`
pattern.

### 4. Capstone — live exercise + portability report

Operator-run on the real remote spine (`tests/integration/live_stack/spine.py`):

- Exercise all four methods against a live remote System: gdbstub attach, console capture
  across a crash, host_dump of a live guest, kdump across an NMI crash.
- Update `just m2-report` so the portability report records remote at **4/4** capture methods.
- Extend the remote runbook with the four-method capture walkthrough.
- Record the `#198` disposition: not deprecated; reframed as default-vs-opt-in.

Consistent with the live-stack-on-hardware constraint, the four-method exercise is the
**capstone runbook on real hardware**, not a CI gate. CI keeps the injected-seam unit tests.

## Components & seams

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `remote_libvirt/retrieve.py::capture` (extended) | dispatch host_dump alongside kdump | `virDomainCoreDumpWithFormat`, `virStorageVolDownload` (injected) |
| `remote_libvirt/console_collector.py` (new) | per-System OpenConsole streamer + buffer | `virDomainOpenConsole` (injected), object store |
| `reconciler/loop.py` (extended) | new collector-supervision repair class | console-collector port |
| `providers/composition.py::build_remote_runtime` | advertise all four methods | — |
| capstone test + `just m2-report` + runbook | prove + document parity | the live remote spine |

Every libvirt seam (`open_connection`, core-dump, volume download, console stream) is injected,
matching the remote provider's existing `open_connection` / `store_factory` /
`agent_exec_factory` discipline so the realization is unit-testable without a host.

## Error handling

- host_dump: a failed dump/download surfaces as `INFRASTRUCTURE_FAILURE`; an over-ceiling core
  is `CONFIGURATION_ERROR` (parity with kdump's `_parse_inspect`); the host volume is always
  deleted in `finally`.
- console collector: a stream drop is recoverable — the supervising pass restarts the
  collector; a System that has disappeared has its collector reaped, not retried forever.
  Collector failures are isolated per-System in the reconcile report (a stuck collector for one
  System never blocks the pass), mirroring `reconcile_once`'s per-repair isolation.
- gdbstub: selection of an unsupported method on a runtime that doesn't advertise it stays the
  existing `CONFIGURATION_ERROR`.

## Testing strategy

- **Unit (CI):** inject fake libvirt streams/volumes/console handles. host_dump: dump →
  download → ceiling enforcement → volume cleanup on both success and failure. Console
  collector: start/restart/reap supervision transitions; buffer rotation; finalize→artifact.
  gdbstub: advertisement + selection confirmation.
- **Live (capstone runbook, real hardware):** all four methods end-to-end on the remote spine.
- **Mutation/edge:** the `finally` volume cleanup and the collector-reap branch are the
  high-value edges — assert a forced download failure still deletes the volume, and a
  vanished System still drops its collector.

## Decomposition

Three independent issues (parallel `/work-issue` agents) + one serialized capstone — the M2
`#207` pattern.

1. **Remote host_dump capture** — `capture()` host_dump path, advertise `HOST_DUMP`,
   ADR-0094. Medium. Self-contained in `remote_libvirt/retrieve.py` + composition.
2. **Remote gdbstub advertisement** — advertise `GDBSTUB`, selection-confirmation test. Small.
3. **Reconciler console collector** — new collector subsystem + supervision pass, advertise
   `CONSOLE`, ADR-0095. Large.
4. **M2.5 capstone** — live four-method exercise, `just m2-report` → remote 4/4, runbook,
   `#198` disposition. Depends on 1–3.

## Open questions / follow-ups

- **>5 GiB cores:** multipart upload for host_dump and kdump alike — a shared follow-up, out of
  M2.5 scope.
- **Console buffer durability:** a hard reconciler kill loses the unflushed buffer since the
  last rotation — and the at-risk bytes are the crash tail, the part that matters most. The
  crash-marker flush + small rotation threshold narrow the window; host_dump/kdump remain the
  durable crash-core path. A fully durable console journal is a follow-up if it proves necessary.
- **`#198` final disposition:** decided post-parity, informed by the capstone — keep local as
  production default vs. reclassify as opt-in dev/CI/reference provider.
