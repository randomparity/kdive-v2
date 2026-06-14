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
`local-libvirt` advertises exactly three: `{CONSOLE, HOST_DUMP, GDBSTUB}`
(`composition.py:132`). The advertised set is load-bearing: `vmcore.fetch` rejects any method
`not in supported_capture_methods` (`vmcore.py`), so KDUMP — which local's Retrieve plane has a
code branch for but does **not** advertise — is not tool-admitted on local, and the two providers'
advertised sets are **disjoint** (`{KDUMP}` vs `{CONSOLE, HOST_DUMP, GDBSTUB}`). `#198` proposed
deprecating local-libvirt after M2; investigation (see the `m25-remote-capture-parity` design
note) found that premise false. The two providers are **near-complementary on capture, not
redundant**, and local-libvirt is the only zero-hardware default/CI/reference provider. The real
gap is that remote cannot yet capture a crash three of the four ways the platform supports.

Scope note: M2.5 brings **remote to 4/4** advertised methods; it does **not** make the two
providers identical — local intentionally does not advertise KDUMP, so post-M2.5 remote
advertises a method local does not. "Parity" here means remote-reaching-4/4, not
remote-matching-local.

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
existing tool. `console` and `gdbstub` are advertised but consumed off the boot/connect planes,
not `vmcore.fetch` — the artifact/transport *shape* matches local. One readiness nuance differs
(per ADR-0095): local consumes the console for **boot readiness**, whereas remote proves
readiness via boot_id-change and treats the console artifact as a crash/diagnostic record only.

### 1. host_dump on remote (ADR-0094)

A new Retrieve-plane path in `remote_libvirt/retrieve.py`, parallel to the kdump `capture()`:

1. `virDomainCoreDumpWithFormat(path, format, flags)` with `flags = VIR_DUMP_MEMORY_ONLY`
   dumps the **crashed** guest's memory to a path **inside the `storage_pool` directory**, named
   deterministically per System (`vmcore.fetch` admits only `SystemState.CRASHED`, like kdump;
   host_dump's distinction is the host-side dump mechanism — no in-guest kdump kernel needed — not
   the System state). A stale same-named volume is deleted first.
2. `pool.refresh()` so libvirt discovers the file as a managed volume, then
   `storageVolLookupByName`. The **5 GiB ceiling is enforced here, against the volume's reported
   capacity, before any download** — an over-ceiling core is rejected having paid only the dump,
   never a multi-GB stream that could OOM the worker. Only then does `virStorageVolDownload`
   stream the (bounded) volume back over the TLS connection.
3. The download **spools to a worker-local temp file** (not held in RAM — a 5 GiB core × concurrent
   captures would OOM the worker); all passes stream over that file at constant memory: sha256,
   the kernel build-id from the **compressed-kdump container's VMCOREINFO** (via drgn — not an
   ELF-note walk; `CaptureOutput.vmcore_build_id` is mandatory), dmesg redaction (shared path),
   and the upload to the object store directly from the file (the worker has the core locally, so
   no presigned-PUT round trip — unlike kdump).
4. The temp file and the host volume are deleted in a `finally` (graceful path); volumes orphaned
   by a non-graceful worker/host crash that bypasses `finally` are reaped by a reconciler sweep.

**Dump format:** ELF memory-only (`VIR_DUMP_MEMORY_ONLY` +
`VIR_DOMAIN_CORE_DUMP_FORMAT_RAW`) is the default. This reverses the original kdump-zlib choice:
a real-hardware run (#319) showed QEMU's compressed-kdump dumps carry `utsname.machine="Unknown"`
and drgn cannot resolve their architecture (`KDUMP_ATTR_ARCH_NAME` unset), so they are
unreadable, whereas drgn reads the ELF format's architecture from `e_machine`. The memory-only
flag is mandatory: without it `format` is ignored and libvirt emits an unreadable QEMU save
image. `KDUMP_ZLIB` stays available behind a config knob. The cost of ELF is size — ≈ full guest
RAM, uncompressed — so host_dump is bounded by the 5 GiB ceiling until multipart upload lands
(see ADR-0094). The provision domain must also enable `<acpi/>` + `<vmcoreinfo state='on'/>` so
the guest populates VMCOREINFO into the dump.

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
parity — boot-through-crash capture — requires a **long-lived** owner that opens the stream
*promptly* (a libvirt stream can't be handed worker→leader, so on remote it opens at a
sub-tick attach-watcher latency, not literally at domain start) and tees continuously. A worker
job cannot host this (it runs one provider operation and returns; holding a stream open for the
System's life would pin a worker slot). The owner is the **reconciler process** — a continuous
attach-watcher opens streams and a new `reconcile_once` class does liveness/reap:

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

Across M2.5 the reconciler gains **two** new `reconcile_once` classes — the host_dump
orphan-volume reap (issue 1) and this console liveness/reap (issue 3) — plus the continuous
attach-watcher (**not** a pass class) and the hosting leader lock, and a new reconciler→provider
seam (a console-collector port) following the `register_with_reaper` pattern.

### 4. Capstone — live exercise + portability report

Operator-run on the real remote spine (`tests/integration/live_stack/spine.py`):

- Exercise all four methods on the live remote spine: gdbstub attach (any running System);
  console capture across a crash (any System); host_dump and kdump **each on its own crashed
  System** — both are vmcore methods requiring `SystemState.CRASHED`, and `ensure_method_match`
  (#118) makes the first method win per System, so the two cannot share one System (crash A →
  host_dump, crash B → kdump). A crashed kernel exports VMCOREINFO reliably; an absent VMCOREINFO
  is the documented `CONFIGURATION_ERROR`, not a 4/4 pass.
- Update `just m2-report` so the portability report records remote at **4/4** capture methods.
- Extend the remote runbook with the four-method capture walkthrough.
- Record the `#198` disposition: not deprecated; reframed as default-vs-opt-in.

Consistent with the live-stack-on-hardware constraint, the four-method exercise is the
**capstone runbook on real hardware**, not a CI gate. CI keeps the injected-seam unit tests.

## Components & seams

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `remote_libvirt/retrieve.py::capture` (extended) | dispatch host_dump alongside kdump | `virDomainCoreDumpWithFormat`, `virStorageVolDownload` (injected) |
| `remote_libvirt/console_collector.py` (new) | per-System OpenConsole streamer + rotation/redaction + part assembly | `virDomainOpenConsole` (injected), object store |
| `reconciler/loop.py` (extended) | host_dump orphan-volume reap sweep (issue 1); console leader-lock + attach-watcher + liveness/reap pass (issue 3) | console-collector port, `pg_advisory_lock` |
| `providers/composition.py::build_remote_runtime` | advertise all four methods | — |
| capstone test + `just m2-report` + runbook | prove + document parity | the live remote spine |

Every libvirt seam (`open_connection`, core-dump, volume download, console stream) is injected,
matching the remote provider's existing `open_connection` / `store_factory` /
`agent_exec_factory` discipline so the realization is unit-testable without a host.

## Error handling

- host_dump (ADR-0094): a failed dump/download surfaces as `INFRASTRUCTURE_FAILURE`. The dump
  format is ELF (`RAW`), a universally-supported libvirt format, so there is no host dump-format
  capability preflight (#319). Three `CONFIGURATION_ERROR`s are raised **before** wasting a
  dump/stream: the `storage_pool` isn't filesystem/`dir`-backed (pool-type preflight), the
  volume's capacity exceeds the 5 GiB ceiling (checked post-refresh, pre-download), or the
  crashed kernel exported no VMCOREINFO so the mandatory `vmcore_build_id` can't be extracted
  (rare for a crashed System, but handled). Volume cleanup: the `finally`
  covers the **graceful** path only — a non-graceful
  worker/host crash bypasses it, so a delete-stale-before-dump step plus a reconciler reap (with
  a live-holder/mtime guard so it never deletes a volume an in-flight capture is streaming) close
  the orphan surface.
- console collector (ADR-0095): a stream drop is recoverable — the liveness/reap pass restarts a
  dead collector; a gone System has its collector reaped **only after** any teardown-finalize has
  persisted the artifact (reap never races finalize). Per-System collector failures are isolated
  in the reconcile report (a stuck collector never blocks the pass). Hosting is single-leader
  (session-scoped advisory lock); on leader failover the standby cold-starts collectors and
  pre-failover console history is lost (future-only `OpenConsole`) — an accepted best-effort
  limitation, with host_dump/kdump as the durable crash-core path.
- gdbstub: selection of an unsupported method on a runtime that doesn't advertise it stays the
  existing `CONFIGURATION_ERROR`.

## Testing strategy

- **Unit (CI):** inject fake libvirt streams/volumes/console handles.
  - host_dump: the dump uses RAW (ELF) memory-only; each preflight `CONFIGURATION_ERROR` fires
    before a dump (non-`dir` pool; over-ceiling volume capacity rejected post-refresh/pre-download;
    no-VMCOREINFO build-id); spool-to-temp-file (not in-RAM); delete-stale-before-dump;
    redact-dmesg; upload.
  - console collector: attach-watcher opens a stream for a System lacking a collector;
    liveness restart of a dead stream; redaction at the **rotation boundary** including a
    secret straddling the part seam; kdive-side part assembly into one artifact; single-leader
    hosting (a non-leader replica / `ops.reconcile_now` hosts none).
  - gdbstub: advertisement + selection confirmation.
- **Live (capstone runbook, real hardware):** all four methods end-to-end on the remote spine.
- **Mutation/edge — the race guards are the highest-value edges:** a forced download failure
  still deletes the volume; the reap **does not** delete a volume an in-flight capture is
  streaming (live-holder guard); the reap **does not** drop a collector before its
  teardown-finalize persists the artifact (reap-after-finalize); a vanished System still drops
  its collector.

## Decomposition

Three logically-separable issues (parallel `/work-issue` agents) + one serialized capstone — the
M2 `#207` pattern — with **two shared-file collision zones**, so the feature merges are
serialized even though the work runs in parallel:
- **`composition.py::build_remote_runtime` — all of 1/2/3.** Each adds a member to the same
  `supported_capture_methods` frozenset (`HOST_DUMP`/`GDBSTUB`/`CONSOLE`), a guaranteed three-way
  conflict. Mitigation: land the full `{CONSOLE, HOST_DUMP, GDBSTUB, KDUMP}` advertisement as a
  tiny **shared prerequisite (issue 0)** the feature issues build behind, so they don't each
  edit the frozenset; or serialize the three merges on that line.
- **`reconciler/loop.py` — issues 1 and 3.** Both add a `reconcile_once` class (the recurring
  rebase zone from prior milestones); the second to land rebases onto the first's reconciler
  changes. They are not logically coupled on leadership: issue 1's orphan-volume reap is a
  **stateless, replication-safe sweep** (idempotent delete + live-holder/mtime guard), whereas
  issue 3 introduces the **single-leader hosting** lock + attach-watcher; only issue 3 needs
  leadership, so issue 1 does not block on it.

1. **Remote host_dump capture** — `capture()` host_dump path, advertise `HOST_DUMP`,
   ADR-0094. Medium. `remote_libvirt/retrieve.py` + composition + a stateless reconciler
   orphan-volume reap sweep (shares `reconciler/loop.py` with issue 3).
2. **Remote gdbstub advertisement** — advertise `GDBSTUB`, selection-confirmation test. Small.
3. **Reconciler console collector** — new collector subsystem + attach-watcher + leader-lock +
   liveness/reap pass, advertise `CONSOLE`, ADR-0095. Large (shares `reconciler/loop.py` with
   issue 1).
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
