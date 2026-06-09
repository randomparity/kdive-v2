# ADR 0078 — Object-store + presigned-URL in-target install/retrieve seam (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  remote-libvirt package this seam serves), [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (the object store + client interface the
  channel uses), [ADR-0030](0030-install-boot-plane.md) (the install/boot plane this redefines
  for a remote target), [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the vmcore
  retrieve plane this reroutes), [ADR-0051](0051-install-method-conditional-crashkernel.md) /
  [ADR-0061](0061-boot-cmdline-composition.md) (the crashkernel/cmdline composition the
  in-guest install reuses).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md)

## Context

`local_libvirt` installs a kernel by copying the vmlinuz to a host path and using QEMU
**direct-kernel boot** (`<kernel>` in the domain XML), and retrieves a vmcore from the host's
kdump path — both rely on a **shared filesystem** the M2 worker no longer has. With
`qemu+tls://` chosen for control (ADR-0077), the control channel offers no command execution
and no general file transfer, so something must (1) get the built kernel onto a target that can
boot it and (2) get the vmcore back out.

The decision must not be made M2-locally. The later milestones constrain it:

- **M3 cloud** — there is no hypervisor host the platform controls; provisioning is
  cloud-image/QCOW2 and install is "image bake / SSH push" (`top-level-design.md` line 233);
  retrieve is "remote vmcore fetch."
- **M4 bare metal** — the target **pulls** its boot artifacts over the network (PXE/NIM); the
  "host" is a BMC, not a Linux box that can run a platform daemon; install is "netboot / SSH
  push"; retrieve is "remote vmcore fetch, BMC SOL capture."
- **M5 PowerVM** — NIM/PXE-provisioned LPARs running Linux.

The one constant from M2 to M5 is **a Linux target instance that can pull an artifact and
install/kexec it**, and the **object store** is the only artifact channel named in every
milestone. "Land a vmlinuz on a host path for direct-kernel boot" exists only at M2, and a
host-side platform daemon has no host to run on in cloud and no Linux host on bare metal.

## Decision

We will make the **object store the canonical artifact channel** for remote-libvirt and every
later provider, and move artifacts via **bounded presigned URLs that the target pulls/pushes**,
not credentials planted in the target:

1. The worker **publishes** the built kernel to the object store (ADR-0013 layout) and mints a
   **presigned GET** URL bounded to the op's lifetime. The object store today exposes only
   `presign_put` (`store/objectstore.py`), so M2 adds a **`presign_get`** primitive — the one
   allowlisted, provider-agnostic core touch-point this seam requires (ADR-0076).
2. The worker **registers the minted URL in the redaction registry** (`registry.register`, the
   ADR-0073 contract applied to a *minted* capability, not a *resolved* secret) and then
   delivers the **URL** (not the bytes, not a standing credential) to the target through an
   **in-target execution seam** — realized for M2 by **qemu-guest-agent `exec` over the
   `qemu+tls://` connection**; M3 re-realizes it via cloud-init/agent, M4/M5 via SSH/SoL after
   netboot — behind the same Installer/Retriever port contract. Registration **before** the
   exec is load-bearing: a presigned URL is a bearer capability, so an unregistered URL captured
   into a persisted transcript would be a live read/write grant until expiry; registering it
   makes the redactor mask it by exact value, and the scope releases only after redact-and-persist.
3. The target **pulls and installs in-guest** (boot entry + the method-conditional crashkernel
   cmdline, reusing ADR-0051/ADR-0061 into the guest grub) and reboots/kexecs into the kernel.
4. On crash, kdump writes the vmcore to the guest's **local dump storage** (the capture kernel
   is a minimal initramfs and is not assumed to reach the object store); on the **next normal
   boot** the in-guest agent uploads it to a **presigned PUT** URL, and the worker references the
   object and runs postmortem locally. The PUT URL is scoped to one object + checksum
   (`PresignPutRequest`) with a lifetime that **covers the crash→reboot→upload window** (or is
   re-minted post-reboot) — a deliberate exception to "shortest possible expiry," bounded to a
   single write of a single object so the longer life is not a broad grant.

**Direct-kernel boot is retired as a carry-forward**: `remote_libvirt` boots a disk-image base
OS and iterates kernels by in-guest install + reboot, debugged via the QEMU gdbstub (ADR-0079).
`local_libvirt` keeps direct-kernel boot until it is removed (ADR-0076).

## Consequences

- **The mechanism survives to M5.** Cloud-init pulling a presigned URL is the cloud-image
  model; a netbooted machine pulling+installing a kernel is the bare-metal model; both reuse
  this seam's contract rather than replacing it — which is the portability the roadmap bets on.
- **No standing credential in any guest, no host agent.** Presigned URLs are capability-scoped
  to one object and time-boxed (the GET to the op's lifetime; the vmcore PUT to the
  crash→reboot→upload window, still one object + one checksum), so a guest never holds
  object-store creds, and no platform daemon is deployed on a host that would not exist in
  cloud/bare-metal.
- **Install becomes in-guest, matching "many Runs against one persistent System."** A persistent
  base-OS System is provisioned once; each Run installs a kernel and reboots — cheap iteration,
  exactly the loop the domain model describes (`top-level-design.md` §Run).
- **New obligation: the base image carries qemu-guest-agent + a virtio-serial channel + a
  gdbstub-enabled domain**, set by the M2 provisioning profile (spec issue 2). The cloud/bare-
  metal equivalents (cloud-init, post-PXE SSH) are their milestones' obligation.
- **Redaction contract on the seam.** A guest-agent `exec` transcript can echo the presigned
  URL and in-guest command output (the TLS cert is consumed by the libvirt layer and never
  reaches the guest agent — ADR-0077). Because the URL is registered before the exec (Decision
  step 2), it flows the normal redaction path and the scope releases only after
  redact-and-persist (ADR-0075), so the capability does not leak unmasked. **M2 proves the
  transcript exact-value redaction half of the secret contract** — the half the TLS cert
  (never transcript-visible) does not exercise.
- **Cost: more moving parts than a host copy** (publish, presign, in-guest pull, in-guest
  install). Accepted: it is the only model that does not get rebuilt at M3.

## Alternatives considered

- **Host-side helper daemon (`kdive-hostd`)** pulls from S3, lands the kernel for direct-kernel
  boot, pushes the vmcore. Clean for M2. Rejected on generalization: there is no controllable
  host in cloud and no Linux host on a BMC, so it is a libvirt-only component thrown away one
  milestone later — the opposite of "add a provider, core unchanged."
- **`virStorageVolUpload`/`Download` over the TLS control channel** (no object store for bulk).
  Single channel, single secret. Rejected: direct-kernel boot from a storage-pool path does not
  generalize, and it keeps bulk data on the control channel the object store exists to offload.
- **Plant object-store credentials in the guest** so it reads/writes S3 directly. Rejected:
  standing credentials in every guest are a broad secret-sprawl surface; presigned URLs give the
  same capability time-boxed and object-scoped.
- **Keep direct-kernel boot for remote** by streaming the kernel through the guest agent.
  Rejected: it preserves a model nothing past M2 uses and pushes large bytes through a control
  channel sized for commands.
