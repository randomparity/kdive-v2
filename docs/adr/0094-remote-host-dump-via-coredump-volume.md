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

1. `virDomainCoreDumpWithFormat` the guest's memory into a **fresh, deterministically-named
   storage-pool volume** (`VIR_DOMAIN_CORE_DUMP_FORMAT_RAW` — an ELF core drgn/crash read
   directly).
2. `virStorageVolDownload` streams the volume to the worker, which computes sha256/size,
   enforces the **5 GiB single-PUT ceiling** (ADR-0048, parity with kdump), extracts + redacts
   dmesg, and uploads the core to the object store **directly** — the worker holds the bytes,
   so no presigned-PUT round trip (the kdump asymmetry: kdump uploads from inside the recovered
   guest; host_dump's guest is dead).
3. Delete the host volume in a `finally`, so a failed download never leaks a multi-GB volume in
   the operator's pool.

Advertise `HOST_DUMP` in the remote runtime's `supported_capture_methods`. Because `HOST_DUMP`
is already in `vmcore.fetch`'s `_VMCORE_METHODS`, the advertisement alone admits it through the
existing tool — no MCP-seam change.

All libvirt seams (core-dump, volume create/download/delete) are injected, matching the remote
provider's `open_connection` / `store_factory` discipline, so the path is unit-testable without
a host.

## Consequences

- Remote reaches host_dump parity with local over a pure libvirt-TLS channel; no host access is
  added.
- The full core streams **through the worker** (unlike kdump's in-guest upload), so the worker
  briefly handles up to 5 GiB. Bounded by the existing ceiling; >5 GiB is a multipart follow-up.
- A new failure surface — a leaked host volume — is closed by the `finally` delete; the
  high-value test asserts cleanup on a forced download failure.
- ADR-0084's `{KDUMP}`-only stance is no longer accurate; this ADR records the supersession so a
  future reader does not treat host_dump-on-remote as unsupported.
