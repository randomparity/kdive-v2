# M2.5 — Remote-libvirt capture-method parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: each sub-issue below is implemented end-to-end
> with the `/work-issue` skill (scope → design → TDD → adversarial-review loops → PR → CI →
> merge), the repo's standard per-issue execution path. This milestone plan is the
> **orchestration layer**: it sequences the issues, names the shared-file collision zones and
> their merge order, and gives each issue its acceptance criteria. Per-task TDD breakdown lives
> inside each `/work-issue` run, not here. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `remote-libvirt` from one advertised crash-capture method (`{KDUMP}`) to all four
(`{CONSOLE, HOST_DUMP, GDBSTUB, KDUMP}`), each realized over the existing libvirt-TLS API with no
new host access, and prove all four against the live remote spine.

**Architecture:** host_dump lands as a new Retrieve-plane path in `remote_libvirt/retrieve.py`
(core-dump → storage-pool volume → stream download → spool → upload, ADR-0094). console lands as
a new `remote_libvirt/console_collector.py` streamer hosted by a single leader-locked reconciler
with a continuous attach-watcher (ADR-0095). gdbstub is already wired and only needs
advertisement. Each method's advertisement (a member added to `supported_capture_methods` in
`composition.py::build_remote_runtime`) is the **last** commit of its issue, gated behind a
working path, so the tool never admits a method that isn't implemented.

**Tech Stack:** Python 3.13, `uv`/`ruff`/`ty`/`pytest`; libvirt-python (`virDomainCoreDumpWithFormat`,
`virStorageVolDownload`, `virDomainOpenConsole`); Postgres advisory locks (`pg_advisory_lock`);
MinIO/S3 object store; drgn (compressed-kdump VMCOREINFO read).

**Spec:** `docs/superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md`
**ADRs:** `docs/adr/0094-remote-host-dump-via-coredump-volume.md`,
`docs/adr/0095-reconciler-remote-console-collector.md`
**Milestone:** M2.5 (GitHub #12)

---

## File structure

| File | Issue | Responsibility | Test |
|------|-------|----------------|------|
| `src/kdive/providers/remote_libvirt/retrieve.py` (modify) | 1 | host_dump `capture()` branch alongside the kdump branch | `tests/providers/remote_libvirt/test_retrieve_host_dump.py` |
| `src/kdive/reconciler/loop.py` (modify) | 1 | host_dump orphan-volume reap sweep | `tests/reconciler/test_loop.py` (reap cases) |
| `src/kdive/providers/composition.py` (modify) | 1, 2, 3 | each adds its method to `build_remote_runtime`'s `supported_capture_methods` frozenset | `tests/providers/test_composition.py` |
| `src/kdive/providers/remote_libvirt/console_collector.py` (create) | 3 | per-System OpenConsole streamer + rotation/redaction + part assembly | `tests/providers/remote_libvirt/test_console_collector.py` |
| `src/kdive/db/locks.py` (modify) | 3 | **new session-scoped** `pg_advisory_lock` helper (distinct from `advisory_xact_lock`) — net-new infra | `tests/db/test_locks.py` |
| `src/kdive/reconciler/loop.py` (modify) | 3 | console liveness/reap `reconcile_once` class | `tests/reconciler/test_loop.py` (console cases) |
| `src/kdive/reconciler/console_hosting.py` (create) | 3 | injectable hosting loop: acquire session leader lock → host collectors → continuous attach-watcher (the testable logic) | `tests/reconciler/test_console_hosting.py` |
| `src/kdive/__main__.py` (modify) | 3 | thin wiring only: `_run_reconciler` constructs the hosting loop with a dedicated leader connection and runs it **concurrently** with `Reconciler.run`, sharing the `stop` event (cancel-on-stop) | — (process entry; not unit-tested by convention) |
| `tests/integration/live_stack/spine.py` (modify) + runbook + `just m2-report` | 4 | live four-method exercise; portability report → remote 4/4 | the live exercise *is* the test (env-gated) |

All libvirt seams are constructor-injected (matching the remote provider's existing
`open_connection` / `store_factory` / `agent_exec_factory` discipline) so every path is
unit-testable with fakes and no host.

**Net-new infrastructure in issue 3 (do not under-scope):** the session-scoped leader lock does
**not** exist today — `db/locks.py` has only the transaction-scoped `advisory_xact_lock`
(`pg_advisory_xact_lock`), which releases at commit and so cannot hold leadership across
between-pass streamers (ADR-0095). Issue 3 must (a) add a session-scoped `pg_advisory_lock`
helper, (b) hold it on a dedicated long-lived connection in the reconciler process
(`__main__._run_reconciler` builds the pool with `min_size=1` borrow-per-repair today — the
leader connection is separate), and (c) run the continuous attach-watcher/hosting loop as a
**second concurrent task** beside the existing 30s `Reconciler.run` interval loop, sharing the
`stop` event and cancelled on shutdown (the worker's `_cancel_aux_tasks` pattern). The hosting
loop lives in an injectable `reconciler/console_hosting.py` unit (directly tested with fakes);
`__main__` only constructs, starts, and cancels it. The liveness/reap class in `loop.py` is the
smaller half.

## Collision zones & merge order

Two shared files force **serialized merges** even though the three feature issues are worked in
parallel:

1. **`composition.py::build_remote_runtime` `supported_capture_methods` — issues 1, 2, 3.** Each
   adds one enum member to the same frozenset. The second and third to merge rebase onto the
   prior member. Each issue adds its member **only as its final commit**, gated behind a working
   capture path (never advertise an unimplemented method — `vmcore.fetch` would admit it and the
   capture would `NotImplemented`).
2. **`reconciler/loop.py` (+ `ReconcileReport`) — issues 1 and 3.** Both add a `reconcile_once`
   repair class. Not logically coupled on leadership: issue 1's orphan-volume reap is a
   **stateless, replication-safe sweep** (idempotent delete + live-holder/mtime guard); issue 3
   introduces the **single-leader hosting** (`pg_advisory_lock`) + attach-watcher. The second to
   land rebases onto the first's reconciler changes.

Merge discipline (the M2.2/M2.3 playbook): land issue 2 (smallest, frozenset-only) first to
establish the frozenset edit pattern, then serialize 1 and 3's merges, rebasing the second.

## Waves

```
Wave 1 (parallel /work-issue):  #issue-1 host_dump   #issue-2 gdbstub   #issue-3 console
Wave 2 (serialized capstone):   #issue-4  (depends on 1, 2, 3)
```

**Parallel-execution isolation (required).** Each Wave-1 `/work-issue` agent runs in its **own git
worktree outside the repo tree** (e.g. `../kdive-worktrees/<branch>`), never nested inside the
working copy — a nested worktree gets walked by whole-tree `ruff`/`ty`/pytest discovery, so one
agent's in-flight errors fail another agent's commit. The two collision zones above
(`composition.py` frozenset; `reconciler/loop.py`) are resolved at **merge** time, not in the
worktrees: each agent edits its own branch freely, and the merges serialize (issue 2 first, then
the second of 1/3 rebases).

---

## Issue 1 — Remote host_dump capture (ADR-0094)

**Size:** Medium. **Files:** `remote_libvirt/retrieve.py`, `reconciler/loop.py` (reap sweep),
`composition.py` (advertise `HOST_DUMP`, final commit). **Label:** `area:providers`.

**Scope.** A `host_dump` branch in `RemoteLibvirtRetrieve.capture()` parallel to the kdump branch:
core-dump the guest memory (memory-only, compressed kdump) into a storage-pool volume, stream it
back, spool to a temp file, extract build-id + redact dmesg at constant memory, upload; plus a
reconciler sweep that reaps orphaned dump volumes.

- [ ] **AC1 — dump invocation.** `capture(system_id, HOST_DUMP)` calls
  `virDomainCoreDumpWithFormat(path, VIR_DOMAIN_CORE_DUMP_FORMAT_KDUMP_ZLIB, VIR_DUMP_MEMORY_ONLY)`
  into a deterministically-named path inside the `storage_pool` directory, deleting a stale
  same-named volume first. (Test: fake conn records the format+flags+path; stale volume deleted.)
- [ ] **AC2 — host-capability preflight.** A host whose `getDomainCapabilities`/dump-format set
  lacks `KDUMP_ZLIB` raises `CONFIGURATION_ERROR` naming the missing capability, **before** any
  dump. (Test: fake host without kdump-zlib → CONFIGURATION_ERROR; no dump call.)
- [ ] **AC3 — pool-type preflight.** A non-`dir`/filesystem `storage_pool` raises
  `CONFIGURATION_ERROR` before dumping. (Test: fake LVM-type pool → CONFIGURATION_ERROR.)
- [ ] **AC4 — pre-download ceiling.** After `pool.refresh()` + `storageVolLookupByName`, a volume
  whose capacity exceeds the 5 GiB ceiling raises `CONFIGURATION_ERROR` **before** `download`.
  (Test: fake volume capacity > 5 GiB → CONFIGURATION_ERROR; `volDownload` never called.)
- [ ] **AC5 — spool, not RAM.** The download spools to a temp file; sha256/build-id/redaction/upload
  stream over the file. (Test: assert the store receives the bytes; a fake asserting it's never
  handed the whole core as one in-memory buffer — i.e. upload reads from a path/stream.)
- [ ] **AC6 — build-id from compressed container.** `vmcore_build_id` is extracted from the
  compressed-kdump VMCOREINFO (drgn path), and a core with **no** VMCOREINFO raises
  `CONFIGURATION_ERROR` (no fabricated empty build-id). (Test: fake core w/ and w/o VMCOREINFO.)
- [ ] **AC7 — graceful cleanup.** On success and on a forced download failure, the temp file and
  the host volume are both deleted (`finally`). (Test: inject a download error; assert
  `vol.delete()` + temp unlink still ran.)
- [ ] **AC8 — orphan reap (reconciler).** A new stateless reap sweep in `reconcile_once` deletes
  dump volumes whose owning System has **no active capture job** and whose mtime exceeds a grace
  window; it does **not** delete a volume an in-flight capture is streaming (live-holder guard).
  (Test: orphan reaped; live-held volume skipped.)
- [ ] **AC9 — advertise (final commit).** Add `HOST_DUMP` to `build_remote_runtime`'s
  `supported_capture_methods`; `vmcore.fetch(method=host_dump)` is now admitted on remote.
  (Test: composition advertises it; `_fetch_vmcore` no longer returns "method not supported".)

**Done when:** AC1–AC9 pass under `just test`; `just lint && just type` clean.

## Issue 2 — Remote gdbstub advertisement

**Size:** Small. **Files:** `composition.py` (advertise `GDBSTUB`). **Label:** `area:providers`.
**Merge first** (establishes the frozenset edit pattern).

**Scope.** The remote gdbstub transport is already wired/exercised (ADR-0083/0085). Advertise it
and confirm selection; no architectural change, no ADR.

- [ ] **AC1 — advertise.** Add `GDBSTUB` to `build_remote_runtime`'s `supported_capture_methods`.
- [ ] **AC2 — advertised + counted, no regression.** A test asserts `GDBSTUB` is in the remote
  runtime's advertised set and is counted by the capability/portability surface (`m2-report`).
  gdbstub is **not** consumed through `vmcore.fetch` (only `HOST_DUMP`/`KDUMP` are), so there is no
  selection path to gate — the assertion is the advertised set + report count, plus a check that
  the existing connect/attach debug path (ADR-0083/0085) is unchanged (no regression).

**Done when:** AC1–AC2 pass; `just lint && just type` clean.

## Issue 3 — Reconciler console collector (ADR-0095)

**Size:** Large. **Files:** `remote_libvirt/console_collector.py` (new per-System streamer),
`reconciler/console_hosting.py` (new injectable hosting/attach-watcher loop), `db/locks.py` (new
**session-scoped** `pg_advisory_lock` helper — net-new), `reconciler/loop.py` (liveness/reap
class), `__main__.py` (thin wiring: leader connection + run hosting loop concurrently with
`Reconciler.run`), `composition.py` (advertise `CONSOLE`, final commit). **Label:**
`area:providers`, `area:core-platform`. **Largest issue — see the net-new-infrastructure note
under File structure.**

**Scope.** A per-System OpenConsole streamer hosted by a single leader-locked reconciler, opened
promptly by a continuous attach-watcher, rotating redacted parts to the object store and
assembling them into one console artifact on finalize.

- [ ] **AC1 — streamer.** `console_collector.py` opens `virDomainOpenConsole`, appends decoded
  output to a bounded buffer, reconnects on stream drop. (Test: fake stream → buffered output;
  drop → reconnect.)
- [ ] **AC2 — rotation + redaction (incl. seam).** On a size threshold the buffer uploads a
  numbered part; **every part is redacted before upload**, with a trailing-overlap re-scan so a
  secret straddling the rotation seam is still caught. (Test: a secret split across the part
  boundary is redacted in the output.)
- [ ] **AC3 — kdive-side assembly.** Finalize (on capture or teardown) reads the ordered parts and
  writes **one** concatenated console artifact in the shape `classify_console`/`read_console_log`
  expect (not S3 multipart). (Test: parts → single object; `classify_console` reads it.)
- [ ] **AC4 — continuous attach-watcher.** The leader opens a stream for any running remote System
  lacking a live collector at sub-tick cadence, decoupled from the 30s repair pass. (Test: a new
  running System gets a collector without waiting for a `reconcile_once` pass.)
- [ ] **AC5 — single-leader hosting.** Hosting is gated by a **session-scoped** `pg_advisory_lock`
  held on a **dedicated connection outside the `min_size=1` repair pool** (holding a pooled
  connection for the process life would pin the pool's only connection and starve repairs) —
  **not** `advisory_xact_lock` (transaction-scoped, which releases at commit and cannot hold
  leadership across between-pass streamers). A non-leader replica and an `ops.reconcile_now`
  (server) invocation host **no** collectors. (Test: the new session-lock helper holds across
  transactions; two fake reconcilers → only the lock-holder opens streams; server-invoked pass
  hosts none.)
- [ ] **AC6 — lock-loss stops hosting (split-brain guard).** A session-scoped advisory lock is
  released by Postgres the instant the holding connection drops, so a standby can acquire it and
  start hosting while the old leader is unaware. The hosting loop therefore monitors its leader
  connection/lock and, on **any** loss (connection error, failed keepalive), **immediately stops
  hosting and closes all open console streams** before any re-acquire attempt — so a failover
  window has at most one host per System, never two streaming concurrently. (Test: simulate
  lock-loss on the fake → the loop closes all streams and opens no new ones until it re-acquires.)
- [ ] **AC7 — liveness/reap class.** A new `reconcile_once` class restarts a dead stream and reaps
  a gone System's collector **only after** any teardown-finalize has persisted the artifact
  (reap never races finalize). (Test: reap-after-finalize ordering; restart of a dead stream.)
- [ ] **AC8 — advertise (final commit).** Add `CONSOLE` to `build_remote_runtime`'s
  `supported_capture_methods`. (Test: composition advertises it.)

**Done when:** AC1–AC8 pass under `just test`; `just lint && just type` clean.

## Issue 4 — M2.5 capstone (live exercise + portability report)

**Size:** Medium. **Files:** `tests/integration/live_stack/spine.py`, the remote runbook,
`just m2-report`. **Label:** `area:providers`, `area:core-platform`. **Depends on 1, 2, 3.**

**Scope.** Operator-run, real-hardware exercise of all four methods; portability report → remote
4/4; runbook; `#198` disposition note. Not a CI gate (live-stack-on-hardware constraint).

- [ ] **AC1 — four-method live exercise.** `spine.py` exercises all four on the live remote spine
  (env-gated, skips cleanly when absent):
  - **gdbstub** — attach to any running System.
  - **console** — lifetime capture across a crash on any System.
  - **host_dump** and **kdump** — each on its **own crashed System**, *not* the same one: both are
    vmcore methods requiring `SystemState.CRASHED` (`vmcore.fetch`, `vmcore.py:212`), and
    `ensure_method_match` (#118/ADR-0050) makes the first captured method win per System, so a
    second vmcore method on the same System is rejected with `CONFIGURATION_ERROR`. Crash System A
    → host_dump (the host-side core-dump path, which needs **no** in-guest kdump kernel); crash
    System B → kdump. (A crashed kernel exports VMCOREINFO reliably; an absent VMCOREINFO is the
    documented `CONFIGURATION_ERROR`, not a pass.)
- [ ] **AC2 — portability report.** `just m2-report` records remote at **4/4** capture methods.
- [ ] **AC3 — runbook.** The remote runbook gains the four-method capture walkthrough.
- [ ] **AC4 — #198 disposition.** Record: local not deprecated; reframed as default-vs-opt-in;
  the two providers' advertised sets stay disjoint (remote advertises KDUMP, local does not).

**Done when:** AC1–AC4 land; the live exercise passes on the operator's remote spine (recorded),
and CI stays green (the exercise is env-gated, not a CI gate).

---

## Milestone exit criteria

1. `build_remote_runtime` advertises `{CONSOLE, HOST_DUMP, GDBSTUB, KDUMP}`; `vmcore.fetch`
   admits `host_dump` on remote.
2. host_dump: each of the four preflight `CONFIGURATION_ERROR`s fires before a wasted
   dump/stream; the core never sits whole in worker RAM; orphan volumes are reaped without
   evicting a live capture.
3. console: a System's console is captured boot→crash via the attach-watcher; rotation parts are
   redacted (incl. seam); a single artifact assembles on finalize; only the leader hosts.
4. All four methods exercised on the live remote spine, recorded operator-run; `just m2-report`
   shows remote 4/4.
5. No `CaptureMethod` vocabulary or MCP-seam change (ADR-0049 untouched); the portability diff
   gate (ADR-0076) stays green.

## Out of scope (own follow-ups)

- **>5 GiB cores** — multipart upload for host_dump and kdump (shared follow-up).
- **Durable console journal** — failover/crash-tail loss is accepted best-effort for M2.5.
- **#198 final disposition** — decided post-parity, informed by the capstone.

---

## Self-review

- **Spec coverage:** §1→Issue 1 (AC1–9), §2→Issue 2, §3→Issue 3 (AC1–8), §4→Issue 4; Non-goals →
  exit criterion 5; Error handling → Issue 1 AC2–7 + Issue 3 AC5–7; Testing → each AC is a test;
  Decomposition collision zones → "Collision zones & merge order". No spec section unmapped.
- **No placeholders:** every AC names a concrete, testable behavior and the guard it exercises.
- **Consistency:** method names (`supported_capture_methods`, `vmcore.fetch`, `reconcile_once`,
  `classify_console`) match the spec, the two ADRs, and the verified code.
