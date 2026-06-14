# local-libvirt provider

The local-libvirt provider runs KDIVE's build, boot, debug, and crash-capture work on the
same host as the worker, driving QEMU/KVM guests through libvirt.

## What it needs

- A working libvirt with the QEMU/KVM stack and an accessible socket (the worker connects
  to `qemu:///system` by default, or `qemu:///session` for an unprivileged session).
- Hardware virtualization (`/dev/kvm`) for usable boot performance.
- A kernel source tree for builds (`KDIVE_KERNEL_SRC`) and disk space for guest overlays,
  artifacts, and captured vmcores.
- The toolchain the build path invokes (`make`, a compiler, and the usual kernel build
  dependencies).

All host-facing settings are in [the config reference](../../guide/reference/config.md).

## Preflight

Before the first run, check the host with:

```bash
just check-local-libvirt
```

The preflight reports missing pieces — libvirt reachability, `/dev/kvm`, the toolchain —
without changing the host.

## End to end

The [live-stack runbook](../runbooks/live-stack.md) walks a full build → boot → verify
cycle over the local provider, including the backend bring-up and the MCP-driven flow.
