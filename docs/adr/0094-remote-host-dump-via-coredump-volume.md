# ADR 0094 — Remote host_dump via core-dump-to-volume + stream download (M2.5)

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** kdive maintainers
- **Supersedes (in part):** [ADR-0084](0084-remote-control-two-phase-vmcore-retrieve.md) — its
  decision that host_dump is host-coupled and therefore unsupported on remote. Everything else
  in ADR-0084 (the Control plane, the two-phase kdump capture) stands.
- **Builds on:** [ADR-0076](0076-remote-libvirt-provider-package.md) (the `remote_libvirt`
  package + portability diff gate this stays inside), [ADR-0080](0080-remote-provisioning-disk-image-profile.md)
  (the `storage_pool` this dumps into and the overlay-volume idiom this reuses),
  [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the Retriever/CrashPostmortem ports this
  extends), [ADR-0049](0049-crash-capture-tiers.md) (the `CaptureMethod` vocabulary, unchanged).
- **Spec:** [`../superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md`](../superpowers/specs/2026-06-11-m25-remote-capture-parity-design.md) §1.

## Context

`local-libvirt` realizes host_dump by `virsh dump`ing the guest to a worker-local path and
reading the file directly — it shares a filesystem with its hypervisor.
`remote-libvirt` shares no filesystem with its host and has no host shell
(`RemoteLibvirtConfig` carries only a `qemu+tls://` URI, mutual-TLS cert refs, and a
`storage_pool`). ADR-0084 concluded from this that host_dump — a **host-side** core, produced
while the guest is dead/panicked and so unable to upload itself the way kdump's recovered guest
does — could not be retrieved on remote, and excluded it: `supported_capture_methods = {KDUMP}`.

That conclusion missed a libvirt-native host-side channel. `virDomainCoreDumpWithFormat` can
target a path **inside the storage pool**, and `virStorageVolDownload` streams a pool volume
back to the worker over the existing TLS connection — no host shell, no new credential.

## Decision

Realize host_dump on remote as a stream pipeline in `remote_libvirt/retrieve.py`, dispatched
from `capture()` alongside the existing kdump path:

1. **Dump.** `virDomainCoreDumpWithFormat(path, format, flags)` with
   `flags = VIR_DUMP_MEMORY_ONLY` and `format = VIR_DOMAIN_CORE_DUMP_FORMAT_KDUMP_ZLIB`,
   writing to a path **inside the configured `storage_pool`'s directory**. The
   `VIR_DUMP_MEMORY_ONLY` flag is what makes libvirt emit a guest-memory dump at all — without
   it, `format` is ignored and libvirt produces a QEMU save/migration image that neither drgn
   nor `crash` can read. Among the memory-only formats, a **compressed kdump** format is the
   default (not uncompressed `RAW`): an uncompressed dump is ≈ the guest's full physical RAM,
   so on any guest with more than a few GiB it would breach the ceiling in step 2 on *every*
   capture; kdump-zlib is drgn-readable (drgn reads makedumpfile-compressed kdumps natively)
   and excludes/compresses free pages, giving cores comparable to the in-guest kdump path. The
   uncompressed `RAW` ELF format stays available behind a config knob for the rare consumer
   that needs it.

   **Host-capability dependency.** `KDUMP_ZLIB` requires the operator's remote host to ship a
   libvirt+QEMU that supports kdump-compressed memory-only dumps. The remote provider runs
   against hosts kdive does not control (ADR-0076), so the capture preflights the host's
   supported dump formats and, if kdump-zlib is unavailable, **fails with a
   `CONFIGURATION_ERROR` naming the missing host capability** (rather than silently emitting an
   unreadable or over-ceiling core). The operator's recourse is the `RAW` knob plus a
   small-enough guest. Auto-falling back to `RAW` is rejected: a silent fallback to a
   ceiling-breaching format trades a clear config error for a confusing capture failure.

   **Pool-type prerequisite.** The dump-to-path → `pool.refresh()` → lookup mechanism (step 2)
   only works for a **filesystem/`dir`-backed** `storage_pool`: an LVM, RBD, or iSCSI pool has
   no directory to write a dump file into and will not discover an externally-written file on
   refresh. `storage_pool` is operator-configured (ADR-0080), so the capture preflights the
   pool type and **fails with a `CONFIGURATION_ERROR`** on an incompatible pool rather than
   dumping into the void. host_dump on remote therefore requires a filesystem-backed pool —
   stated here so it is a named prerequisite, not a silent assumption.
2. **Resolve the volume + enforce the ceiling before streaming.** After the dump,
   `pool.refresh()` so libvirt discovers the newly-written file as a managed volume, then
   `storageVolLookupByName` to obtain the `virStorageVol` handle (the dump writes a *path*;
   `virStorageVolDownload` operates on a *volume object* — the refresh is what bridges the two
   and gives the volume correct size metadata). The **5 GiB single-PUT ceiling** (ADR-0048) is
   enforced **here**, against the volume's reported capacity, *before any download* — an
   over-ceiling guest is rejected with a `CONFIGURATION_ERROR` having paid only the host-side
   dump, not a tens-of-GiB stream that would also risk OOMing the worker (see the sizing caveat
   in §Consequences — for host_dump this bounds the guest RAM the method supports, not a rare
   edge). A post-download streamed-byte count is kept only as a sanity assertion.
3. **Download + store.** `virStorageVolDownload` **spools** the (now known-bounded) volume to a
   worker-local temporary file — it is **not** buffered in RAM. A 5 GiB in-ceiling core held
   resident would already be heavy, and host_dump captures for different Systems can run
   concurrently, so an in-memory core would multiply into worker OOM (which, per step 4, would
   then *also* leak the host volume). All passes stream over the spooled file at constant
   memory: the worker computes sha256, extracts the kernel **build-id from the core's
   VMCOREINFO**
   (`CaptureOutput.vmcore_build_id` is mandatory — `providers/ports/retrieve.py` — and the
   `run_crash_postmortem` provenance check depends on it). Because the default core is a
   compressed kdump, **not** an ELF image, build-id extraction must read VMCOREINFO from the
   makedumpfile container (via drgn, which already parses these), not walk ELF notes — local
   host_dump's ELF-oriented `_read_vmcore_build_id` is therefore **not** reused as-is for the
   compressed path. host_dump runs on a **crashed** System (`vmcore.fetch` admits only
   `SystemState.CRASHED`), and a crashed kernel exports VMCOREINFO reliably; in the rare case the
   note is absent (an image without a vmcoreinfo device / unpopulated note) the capture fails with
   a `CONFIGURATION_ERROR` naming the missing build-id rather than fabricating an empty one that
   would later fail the postmortem provenance check. The worker then extracts + redacts dmesg and uploads the core to the
   object store **directly** from the spooled file (the upload, like the other passes, streams
   from disk) — the worker holds the core locally, so no presigned-PUT round trip (the kdump
   asymmetry: kdump uploads from inside the recovered guest; host_dump's guest is dead). The
   spooled file is removed alongside the host volume in step 4's cleanup.
4. **Clean up.** Delete the host volume in a `finally` for the graceful path. Because a worker
   SIGKILL/OOM/host crash between create and delete bypasses `finally` and would orphan a
   multi-GB volume — and the deterministic per-System name would then collide with the next
   capture's dump — a reconciler sweep reaps orphaned dump volumes (the same orphan-reaping
   shape ADR-0095 gives the console collector).

   **The reap and the pre-delete must not race a live capture.** Both the reconciler sweep and
   the capture's own delete-stale-before-dump step operate on the same deterministically-named
   volume from a different process than the streaming download, so an unguarded delete could
   drop a volume mid-`virStorageVolDownload` (the reconciler-vs-live-holder hazard ADR-0086/#216
   solved with an explicit live-holder guard). Two guards close this:
   - **Per-System capture serialization** — host_dump capture for a System runs under the same
     single-active-capture invariant the vmcore plane already enforces (`ensure_method_match`,
     first-method-wins per System, #118), so two captures never share the name concurrently and
     the pre-delete only ever removes a *prior, finished* orphan.
   - **A live-holder guard on the reap** — the sweep reaps a dump volume only when its owning
     System has **no active capture job** *and* the volume's mtime is older than a grace window,
     so it never deletes a volume a running capture is still writing or downloading.

Advertise `HOST_DUMP` in the remote runtime's `supported_capture_methods`. Because `HOST_DUMP`
is already in `vmcore.fetch`'s `_VMCORE_METHODS`, the advertisement alone admits it through the
existing tool — no MCP-seam change.

Constant-memory streaming over the spooled core requires a **file/stream-based artifact write
path**: today's `ArtifactWriteRequest` carries `data: bytes` (`provider_components/artifacts`),
which would force the whole core back into RAM at upload. Extending the store with a
file/stream-backed put (used by host_dump, available to kdump/console later) is a named
prerequisite of this ADR, not an assumed capability.

All libvirt seams (core-dump, volume create/download/delete) are injected, matching the remote
provider's `open_connection` / `store_factory` discipline, so the path is unit-testable without
a host.

## Consequences

- Remote reaches host_dump parity with local over a pure libvirt-TLS channel; no host access is
  added.
- The full core transits **the worker** (unlike kdump's in-guest upload), spooled to a
  temporary file and streamed at constant memory — not held in RAM — so an in-ceiling core, or
  several concurrent captures, cannot OOM the worker. Bounded by the existing ceiling; >5 GiB is
  a multipart follow-up. This constant-memory path depends on the file/stream artifact-write
  prerequisite noted in §Decision.
- **The 5 GiB ceiling bounds supported guest RAM, not a rare edge.** Even compressed, a
  memory-only dump scales with the guest's RAM, so unlike kdump (where makedumpfile shrinks
  most cores well under the ceiling) host_dump can hit it on a legitimately large guest. The
  ceiling is therefore a *capability limit* of the method until multipart lands, and an
  over-ceiling core is a `CONFIGURATION_ERROR` the operator resolves by sizing the guest or
  using kdump — it is not an error-rate edge to be hand-waved.
- A new failure surface — a leaked host volume — is **mostly** closed: the `finally` delete
  covers graceful failure, a delete-stale-before-dump step covers a single prior orphan, and a
  reconciler sweep covers volumes orphaned by a non-graceful worker/host crash (which bypasses
  `finally`). The high-value tests assert cleanup on a forced download failure **and** that a
  pre-existing stale volume does not wedge the next capture.
- ADR-0084's `{KDUMP}`-only stance is no longer accurate; this ADR records the supersession so a
  future reader does not treat host_dump-on-remote as unsupported.
