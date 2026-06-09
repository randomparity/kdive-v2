# ADR 0079 — Remote live-debug transport reachability (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  remote-libvirt package), [ADR-0077](0077-qemu-tls-control-transport.md) (the control channel
  that does **not** carry the debug transport), [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the in-target seam drgn-live reuses), [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)
  (the gdbstub Connect plane + DebugSession lifecycle), [ADR-0034](0034-debug-plane-gdbmi-tier.md)
  (the gdb-MI tier), [ADR-0033](0033-drgn-introspection-from-vmcore.md) (vmcore drgn),
  [ADR-0039](0039-ssh-transport-live-introspection.md) (the live drgn introspection this routes
  through the guest agent instead of SSH).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md)

## Context

Local-libvirt's debug plane assumes co-location: the QEMU gdbstub is a TCP socket on the same
host as the worker, and live drgn introspection reached the guest over SSH (ADR-0039). On a
remote host neither holds, and `qemu+tls://` (ADR-0077) tunnels neither the gdbstub port nor a
guest shell. The three debug paths have **different** reachability needs and must be reasoned
about separately:

- **gdb-MI tier** (breakpoints, single-step, live `read_memory` / `read_registers`,
  continue/interrupt, ADR-0034) — needs the QEMU **gdbstub TCP port**, which QEMU opens on the
  host. There is no libvirt API to proxy it.
- **drgn live introspection** (ADR-0039) — reads live kernel data structures from inside the
  guest; needs **in-guest execution**, not a host port.
- **vmcore postmortem** (drgn-from-vmcore, crash; ADR-0031/0033) — operates on the vmcore
  **file**; needs no live reachability at all once the object is fetched.

## Decision

We will scope remote live-debug by what must cross the network:

- **gdb-MI: direct TCP from the worker to the host's gdbstub port**, treated as a **security
  boundary, not a firewall convenience**. The QEMU gdbstub is **unauthenticated and
  unencrypted** — whoever can reach the port has full read/write control of the guest kernel —
  so the network ACL **is** the auth: the gdbstub is bound and ACL'd to reach **only the worker
  pool's source addresses** (not "a port range open to the network"), and one System's gdbstub
  is **unreachable by other tenants or guests** (multi-tenant isolation). Each running System
  gets a **distinct gdbstub port**: the provisioning profile (spec issue 2) enables the gdbstub
  and **allocates + records** the port (in System state / `capabilities`, collision-free across
  concurrent Systems on one host); the Connect port (issue 6) reads it and hands the worker's
  gdb a `host:port`. Bare metal later swaps the gdbstub for KGDB-over-SoL **behind the same
  Connect port**. Where a deployment cannot guarantee the ACL, the TLS-tunneled proxy
  (Alternatives) is the hardening path — M2 assumes an operator-controlled worker↔host segment.
- **drgn live introspection: in-guest, via the qemu-guest-agent seam** (ADR-0078) — the worker
  **composes** a constrained, allowlisted drgn invocation and runs it inside the guest, streaming
  results back; no SSH login and no new channel. Two obligations follow: the base image carries
  **drgn + matching vmlinux/debuginfo** for live `/proc/kcore` introspection (a provisioning-
  profile requirement, spec issue 2), and the **constrained-debug allowlist is enforced
  worker-side at script composition** — never trusted to an in-guest shell, which a guest-agent
  `exec` could otherwise bypass.
- **vmcore postmortem: on the worker** — fetch the vmcore object from the store (uploaded via
  the presigned-PUT path, ADR-0078) and run drgn/crash locally; no live reachability.

## Consequences

- **The full M1.2 spine works remotely**, including the "attach" step (gdb-MI over direct TCP),
  so the operator-run e2e (spec issue 8) exercises the real gdb-MI tier on a real remote
  provider — not a narrowed substitute.
- **Operator responsibility is a security control, not just routing.** The ACL restricting the
  unauthenticated gdbstub to the worker pool's source (and reachability of the libvirtd TLS
  port, ADR-0077) is a precondition the operator owns; it is recorded in the spec and the e2e
  runbook as the auth boundary for the debug port, not assumed silently. A gdbstub reachable
  beyond the worker pool is a guest-kernel-takeover exposure.
- **Single-client gdbstub contention must be reconciled, not just detected.** QEMU's gdbstub
  accepts one client; a stale TCP connection from a dead worker can hold it and block re-attach.
  The DebugSession reconciler (ADR-0032, top-level design §Reconciliation) must **reset the
  dead-worker transport**, not merely mark the row `detached`, or the next attach contends with
  a ghost — surfacing as `transport_conflict`.
- **drgn-live needs no SSH on a remote host**, unlike ADR-0039's local path — it rides the same
  guest-agent seam install uses, so M2 adds no second in-guest channel.
- **Unreachable gdbstub / guest agent maps to `transport_failure`; a contended single-client
  gdbstub maps to `transport_conflict`; a reference invalidated by a reboot or reprovision maps
  to `stale_handle`** — all existing categories; no new strings.
- **Cost / limitation: direct TCP assumes a routable, ACL-controlled worker↔host segment.** In
  a segmented network this needs an operator-provided route plus the source-restricting ACL
  above. The TLS-tunneled gdbstub proxy (Decision, Alternatives) is the hardening path when that
  cannot be guaranteed; M2 does not build one (it adds a deployed proxy + cert material not
  obviously reused by cloud/bare-metal, which swap the transport entirely).

## Alternatives considered

- **In-target only — defer remote gdbstub.** Scope M2 live debug to in-guest drgn + vmcore
  postmortem, deferring gdb-MI until a tunnel exists. Smaller, but it leaves the gdb-MI tier
  unproven on a real remote provider and reinterprets the M1.2 spine's "attach" step. Rejected:
  M2's goal is to prove the **full** spine remotely.
- **TLS-tunneled gdbstub proxy** (stunnel fronting the gdbstub port). Keeps everything in one
  TLS trust domain. Rejected for M2: it adds a deployed proxy and per-host cert material,
  over-engineering the common routable case and not obviously reused by cloud/bare-metal (which
  swap the transport entirely).
- **drgn-live over SSH (reuse ADR-0039 against the guest).** Rejected: it reintroduces an SSH
  login the TLS + guest-agent model avoids, and a second in-guest channel beside the one install
  already uses.
