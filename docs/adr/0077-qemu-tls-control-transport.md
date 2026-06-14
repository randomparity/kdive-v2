# ADR 0077 — qemu+tls:// control transport + x509 client-cert secret-by-reference (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  remote-libvirt package this transport serves), [ADR-0012](0012-secret-backend.md) /
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the register-before-return
  `SecretBackend` the cert resolves through), [ADR-0073](0073-forced-secret-resolution-redaction.md)
  / [ADR-0075](0075-objectstore-quarantine-pre-registration-writes.md) (the
  register-before-emit / release-after-persist redaction contract this is the first real
  provider to exercise).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../design/m2-remote-libvirt.md)

## Context

`local_libvirt` connects to `qemu:///system` on the same host (the connection URI is already
read from `KDIVE_LIBVIRT_URI`, `discovery.py`). M2's host is **remote** and the MCP/worker tier
does not share its filesystem or trust domain. libvirt offers two production remote transports:
`qemu+ssh://` (tunnels control and, via the same key, file transfer) and `qemu+tls://` (x509
mutual-TLS to a TLS-listening libvirtd, control only).

Bulk artifact movement is decided separately (ADR-0078: the object store, not the control
channel), which removes the main reason to prefer SSH (its file-transfer convenience). What
remains is the control channel and its secret. M2 is also the **first** provider to resolve a
real secret — local-libvirt resolves none, and M1.5's fault-inject resolved only a synthetic
sentinel (ADR-0073) — so the choice must exercise secret-by-reference resolution and the
on-disk lifecycle that real credential material forces.

## Decision

We will use **`qemu+tls://` for the remote-libvirt control plane** (discovery capability
enumeration, provisioning define/start, control power/reset/force-crash), with **mutual TLS**:
the worker presents an x509 client cert **and** verifies the libvirtd **server** cert against a
configured CA and hostname — `?no_verify=1` is forbidden, so a misconfigured or spoofed host
fails closed rather than connecting unverified.

The **client cert, key, and CA are secrets-by-reference**: the resource's `capabilities` jsonb
carries `secret_ref`s, never the material itself. Because `SecretBackend.resolve()` returns a
**string** but libvirt's TLS client reads cert/key/CA from **on-disk files** in a pkipath, the
worker must, per op: resolve the refs through the runtime's `SecretBackend`, **materialize** the
resolved bytes into a **private, per-op pkipath directory** (mode `0700`, files `0600`), point
the connection at it via the `?pkipath=` URI parameter, and **delete the directory when the op
ends** (success or failure). The resolved values are registered in the redaction registry on
resolution (ADR-0027) as **defense-in-depth** in case any reaches a log line, but the primary
control for the private key is its **bounded on-disk lifetime + guaranteed cleanup**, not text
masking — the key is consumed by the TLS layer and never echoed into a transcript, so masking
alone would not protect it. Bulk files do **not** ride this channel (ADR-0078); the live
gdbstub does **not** ride it either (ADR-0079).

## Consequences

- **Control and bulk data are decoupled.** A large vmcore transfer cannot stall or be coupled
  to the libvirt control channel, and the control channel carries one credential kind (the TLS
  cert), not also an SSH key reused for sftp.
- **M2 proves the real-secret *resolution + materialization + cleanup* path.** The TLS cert is
  the first production secret the platform resolves; M2 exercises resolve → materialize to a
  private pkipath → use → delete against real credential material — a path local-libvirt (no
  secrets) and fault-inject (a synthetic sentinel never written to disk) never ran. The
  **transcript exact-value redaction** half of the contract is proved separately, by the
  presigned URL that *does* flow into the guest-agent transcript (ADR-0078) — not by the TLS
  cert, which the TLS layer consumes and never echoes.
- **New obligation: a private-key-on-disk lifecycle.** Materializing the key to a pkipath
  creates a window where it exists on the worker's filesystem. The op must create it `0700`,
  restrict files `0600`, and delete the directory on every exit path via a guaranteed
  `finally` / context manager (including exceptions and cancellation). The worker's pkipath is
  not reconciler-visible state (the reconciler reconciles provider infrastructure, not
  worker-local temp dirs), so cleanup must be local-and-guaranteed; a worker crash mid-op leaves
  the exposure bounded by the worker's ephemeral storage lifetime. This is the cost of
  `qemu+tls://` over an in-memory secret.
- **Operational cost: TLS material management.** Each remote host needs a libvirtd TLS listener
  and the platform needs a client cert per host (or CA-issued) plus the CA, provisioned into the
  secret backend and referenced by `secret_ref`. This is heavier than an SSH key but is the
  standard multi-tenant libvirt posture and avoids planting an SSH login on every host.
- **Reachability assumption (documented).** The worker must reach the host's libvirtd TLS port;
  this is an operator network responsibility, recorded with the gdbstub-port assumption
  (ADR-0079).
- **A connect/auth failure maps to `transport_failure`** — no new `ErrorCategory`.

## Alternatives considered

- **`qemu+ssh://` (SSH-unified).** Collapses control + file transfer + a gdbstub local-forward
  onto one SSH identity and one channel — operationally simple. Rejected as the M2 base because
  bulk movement is the object store (ADR-0078), which removes SSH's main advantage, and because
  an SSH login on every remote host is a broader standing-access surface than a scoped TLS
  client cert in a multi-tenant service; TLS also matches the "MCP server separate from dev
  hosts" deployment without granting shell access.
- **`qemu+tls://` control + `virStorageVolUpload` for files (no object store).** Single channel,
  single secret. Rejected with ADR-0078: direct-kernel boot from a storage-pool path does not
  generalize past M2, and the object store is the only artifact channel present in every later
  milestone.
- **Unauthenticated `qemu+tcp://`.** Rejected outright — no mutual auth, unacceptable for a
  multi-tenant service reaching hosts over a network.
