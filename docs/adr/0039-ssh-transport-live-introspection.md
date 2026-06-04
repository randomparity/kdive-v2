# ADR 0039 — SSH transport + live drgn introspection (M1)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** M1 — Allocation/accounting depth (live `introspect.run`)
- **Depends on:** [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the Connect
  plane + DebugSession lifecycle this adds a transport to),
  [ADR-0033](0033-drgn-introspection-from-vmcore.md) (the offline drgn path whose live
  sibling this is), [ADR-0012](0012-secret-backend.md) /
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the file-ref secret backend
  the SSH credentials resolve through)
- **Refines:** the Connect/Debug planes of the M0 and M1 specs

## Context

M0's Connect plane is gdbstub-only, and M0 deferred **live** drgn introspection
(`introspect.run`) to M1: "v1 implements it as drgn-over-SSH (`local-drgn-introspect`,
`transports=["ssh"]`), which needs a guest SSH transport + credentials (secret
backend) that the M0 walking-skeleton path does not otherwise require" (ADR-0033).
Only the **offline** path (`introspect.from_vmcore`, drgn on a captured vmcore, no
guest) shipped in M0. M1 adds the live path, which requires the two pieces M0 left
out: an **SSH transport** behind the Connect-plane `Protocol`, and **secret-backed
credentials** resolved through the existing file-ref backend.

This is the largest of M1's swept-in deferrals — a real new transport, not just a
state-machine edge — but it lands entirely behind the existing plane seams.

## Decision

### 1. SSH is a second Connect-plane transport, registered as a capability

The `ConnectPlane` `Protocol` (`open_transport(system, kind) → TransportHandle`) is
unchanged; M1 adds a provider capability `(connect, open_transport, local-libvirt)`
advertising `kind="ssh"` alongside the existing `kind="gdbstub"`. The provider gains
an `ssh` backend (port v1's SSH transport) that opens a connection to the booted
guest. `debug.start_session(run_id, transport="ssh")` dispatches to it by capability
match — **no core change**, the same dispatch that selects gdbstub.

### 2. SSH credentials are **secret-by-reference**, resolved at the worker boundary

The guest SSH credential (key or password) never appears in a request, state row, or
response. The provisioning profile carries a **reference** (`ssh_credential_ref`) into
the file-ref secret backend ([ADR-0012](0012-secret-backend.md)); the worker resolves
it at transport-open time and **registers the resolved value into
`PROCESS_SECRET_REGISTRY` before use** ([ADR-0027](0027-safety-modules-secret-backend-impl.md)),
so any transcript or introspection output capturing it is masked by exact-value
replacement. Output captured before registration completes is quarantined
(`sensitive`) until redacted — the same contract M0 specified but barely exercised; SSH
is the first M1 op to give it real traffic.

### 3. Live `introspect.run` runs drgn over the SSH transport, output redacted

`introspect.run(session_id, helper)` runs the same minimal drgn helper set as the
offline path (tasks, modules, sysinfo) but against the **live** guest kernel over SSH
(`drgn` attached to the running kernel), not a vmcore. It reuses the ported drgn
helpers from [ADR-0033](0033-drgn-introspection-from-vmcore.md); the only difference is
the drgn program source (live kernel vs core file). All output passes through the
redactor before persistence and before any response snippet — identical to the offline
path and to gdb-MI.

### 4. Live introspection binds to a `live` DebugSession over an SSH transport

`introspect.run` requires a DebugSession in `live` on an `ssh` transport (started via
`debug.start_session(run_id, transport="ssh")`), reusing the M0 DebugSession lifecycle
(`attach ↔ live ↔ detached`, durable row, heartbeated). A reboot/crash drops the SSH
transport and ends the session, exactly as it ends a gdbstub session — the
session-per-boot invariant is transport-agnostic. gdbstub (stop-the-world debugging)
and ssh (live introspection of a running kernel) are **different transports for
different operations**; a Run may use either, and the single-attach rule
(`transport_conflict` on a second attach to a `live` session) applies per transport.

## Consequences

- The full debug surface from the top-level design is reachable for local-libvirt:
  offline `introspect.from_vmcore` (M0) **and** live `introspect.run` (M1), plus gdb-MI
  over gdbstub (M0).
- The first real workout of the secret-resolution-and-registration contract under a
  live transport, ahead of M1.5's fault-injection provider that will stress it
  harder — a useful early signal that the contract holds.
- SSH lands purely as a provider capability + a secret reference in the profile; the
  Connect/Debug `Protocol`s and the dispatch core are untouched, a concrete test of
  the "new transport = provider change only" seam.
- New schema is minimal: the `ssh_credential_ref` lives in the provisioning profile
  jsonb (no new column); the DebugSession `transport`/`transport_handle` columns
  already carry an arbitrary transport kind.

## Alternatives considered

- **Carry the SSH credential inline in the request/profile.** Rejected outright by the
  secrets-by-reference cross-cutting rule: credentials never appear in requests, state,
  or responses. The reference + worker-boundary resolution + registry registration is
  the only allowed path.
- **A new live-introspect transport outside the Connect plane.** Rejected: it would
  duplicate the DebugSession lifecycle and bypass capability dispatch. SSH is a Connect
  transport like gdbstub; reusing the seam is the point.
- **Run live introspection over the gdbstub instead of SSH.** Rejected: gdbstub is
  stop-the-world stub debugging; drgn live introspection wants a running kernel it can
  read via `/proc/kcore`-style access over a shell, which is the SSH path v1 already
  implements. They are complementary, not substitutes.
- **Defer live `introspect.run` past M1.** Rejected by the M1 scope decision: M1 sweeps
  the M0 debug-plane deferral so the local-libvirt provider is feature-complete before
  M1.5 starts injecting faults into it.
