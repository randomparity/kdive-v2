# ADR 0060 — Per-System rootfs overlay (a writable qcow2 layer over the shared base)

- **Status:** Proposed
- **Date:** 2026-06-06
- **Depends on:** [ADR-0025](0025-provisioning-plane-libvirt.md) (the provisioning plane this extends),
  [ADR-0030](0030-install-boot-plane.md) (the direct-kernel `root=/dev/vda` boot the overlay
  serves).

## Context

`resolve_rootfs_path` returns the literal base image for a `path`/`url`/`catalog` rootfs, and
provisioning attached that file directly as the domain's writable `vda`. Two Systems that
reference the same base therefore open the same qcow2 read-write, and libvirt/QEMU refuses the
second with `Failed to get "write" lock`. Even when they did not overlap, both wrote into the
one shared image, so one System's state bled into the next. Found driving the live pipeline:
a second System against the demo rootfs could not start while the first held it.

## Decisions

### 1. Each System boots its own qcow2 overlay backed by the base

`provision` calls `qemu-img create -f qcow2 -F qcow2 -b <base> <overlay>` to mint a per-System
overlay at `/var/lib/kdive/rootfs/<system_id>-overlay.qcow2`, and the domain attaches the
overlay (not the base). The base is opened read-only through the backing chain and shared
freely across Systems; all guest writes land in the System's private overlay. The overlay is
created **only when absent**: `provision` is idempotently retried (the already-running domain is
a success post-state), and recreating an overlay a running QEMU holds open would fail the lock or
truncate the live disk — so a present overlay is left in place. `qemu-img`, the overlay removal,
and the presence check are injected seams (`make_overlay`/`remove_overlay`/`overlay_exists`) so
the provisioning unit tests never spawn a subprocess; the real implementations run on a host.

### 2. The overlay's lifecycle is the System's

`teardown` removes the overlay after the libvirt destroy/undefine — on the
already-absent-domain path too — so a torn-down System leaves no orphaned disk; an absent
overlay is a no-op. A failed `provision` reclaims the overlay it created in the same handler
that undefines the half-defined domain, so a failure leaks neither a domain nor a disk.
`reprovision` is `teardown` + `provision`, so it removes the old overlay and mints a fresh one.

### 3. Scope

Leaked-domain reaping (the reconciler's `InfraReaper`, #15) does not yet remove a leaked
domain's overlay; that joins the real reaper when it lands. Overlay-size limits and base-image
garbage collection are out of scope here.
