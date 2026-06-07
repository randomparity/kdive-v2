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

### 1. SSH is a second Connect-plane transport on the typed provider runtime

The handler-facing `Connector` port (`open_transport(system, kind) → TransportHandle`) is
unchanged; M1 adds `kind="ssh"` alongside the existing `kind="gdbstub"` behind the same
typed `ProviderRuntime.connector()` seam (ADR-0063). The provider gains an `ssh` backend
(port v1's SSH transport) that opens a connection to the booted guest.
`debug.start_session(run_id, transport="ssh")` calls
`ProviderRuntime.connector().open_transport(system, "ssh")` — no new MCP lifecycle or
job-dispatch path.

The SSH endpoint resolution is subject to the **same loopback-IP-literal-before-IO
control** the gdbstub transport enforces (ADR-0032 §5, the ported v1 "F2" SSRF
defense): a local-libvirt guest is reached on a loopback-forwarded port, so the
resolved SSH host must be a loopback IP literal and a non-loopback host or a hostname
is a `configuration_error` raised before any connect (reusing `connect.py`'s
`_is_loopback_literal`). SSH is the first transport that opens a long-lived
authenticated outbound connection, so restating the control here is load-bearing, not
incidental.

### 2. SSH credentials are **secret-by-reference**, resolved at the worker boundary

The guest SSH credential (key or password) never appears in a request, state row, or
response. The provisioning profile carries a **reference** (`ssh_credential_ref`) into
the file-ref secret backend ([ADR-0012](0012-secret-backend.md)); the worker resolves
it via `FileRefBackend.resolve(ssh_credential_ref)` and **registers the resolved value
into `PROCESS_SECRET_REGISTRY` before use** ([ADR-0027](0027-safety-modules-secret-backend-impl.md)),
so any transcript or introspection output capturing it is masked by exact-value
replacement.

**Ordering anchor (the acceptance ordering test).** The credential is resolved
**before** `connector.open_transport(system, "ssh")` is called — `FileRefBackend.resolve`
registers the value into the registry before it returns (a structural post-condition of
the backend, `secrets.py`), so the connector that opens the SSH connection runs with the
registry already seeded. The credential registers **process-global** (`scope=None`),
retained for the process lifetime per ADR-0012 (it is a long-lived guest credential, not
a per-request scope). The ordering test asserts the registry contains the resolved value
**before** the connector's first invocation, and that a `Redactor` built after resolution
masks the value — so output captured by the transport (even a *failed* open) is masked.

**Quarantine of pre-registration output.** Because resolution precedes the connector
call, no transport output is produced before the value is registered. The "quarantine"
contract is therefore the safety net for the inverse: any byte stream the SSH transport
captures (the raw command/response transcript of the drgn-over-SSH session) is treated as
`sensitive` — persisted, if at all, only as a `Sensitivity.SENSITIVE` artifact and never
returned un-redacted (decision 3). This is the same contract M0 specified but barely
exercised; SSH is the first M1 op to give it real traffic.

### 3. Live `introspect.run` runs drgn over the SSH transport, output redacted

`introspect.run(session_id, helper)` runs the same minimal drgn helper set as the
offline path (tasks, modules, sysinfo) but against the **live** guest kernel over SSH
(`drgn` attached to the running kernel), not a vmcore. It reuses the ported drgn
**helper functions** (`helper_tasks`/`helper_modules`/`helper_sysinfo`) from
[ADR-0033](0033-drgn-introspection-from-vmcore.md) verbatim — they operate on the narrow
`_Program` `Protocol` and do not know whether the program came from a core or a live
kernel.

**The seam that produces the `_Program` is new.** The offline port opens a drgn
`Program` from a *staged local core + vmlinux* (`open_program(core, vmlinux)`); the live
path instead attaches drgn to the running guest kernel over the SSH transport. This is a
distinct `live_vm`-gated seam (`open_live_program(transport_handle) -> _Program`)
defaulting off-gate to `MISSING_DEPENDENCY`, mirroring the offline seam guard. Its
failure mapping: an SSH/transport fault (auth refused, connection dropped) → the
transport-layer category the SSH backend already returns (`transport_failure` /
`debug_attach_failure`); drgn failing to attach to the live kernel → `debug_attach_failure`
(the attach boundary, as offline §5); per-helper decode skew → the same per-helper error
marker / `all_failed` flag the offline helpers already emit (never escalated to an attach
failure). All output passes through the redactor **inside the port** (the single
redaction boundary, as offline §6) before persistence and before any response snippet —
identical to the offline path and to gdb-MI.

### 4. Live introspection binds to a `live` DebugSession over an SSH transport

`introspect.run` requires a DebugSession in `live` on an `ssh` transport (started via
`debug.start_session(run_id, transport="ssh")`), reusing the M0 DebugSession lifecycle
(`attach ↔ live ↔ detached`, durable row, heartbeated). A reboot/crash drops the SSH
transport and ends the session, exactly as it ends a gdbstub session — the
session-per-boot invariant is transport-agnostic. gdbstub (stop-the-world debugging)
and ssh (live introspection of a running kernel) are **different transports for
different operations**; a Run may use either, and the single-attach rule
(`transport_conflict` on a second attach to a `live` session) applies per transport.

**Per-transport scoping of the single-attach query.** The M0 conflict check
(`debug.py` `_OCCUPIED_SQL`) selects any `attach`/`live` session joined through
`runs.system_id` with **no transport predicate**, so as written it would refuse an ssh
attach on a System that already holds a gdbstub session — contradicting "different
transports may coexist". The query therefore gains an `AND s.transport = %s` predicate
so gdbstub and ssh sessions on one System are independent and only a second **ssh**
attach (the acceptance case) returns `transport_conflict`. The pre-lock fast-fail and
the under-lock authoritative re-check (ADR-0032 §6a) both apply the transport-scoped
predicate.

## Consequences

- The full debug surface from the top-level design is reachable for local-libvirt:
  offline `introspect.from_vmcore` (M0) **and** live `introspect.run` (M1), plus gdb-MI
  over gdbstub (M0).
- The first real workout of the secret-resolution-and-registration contract under a
  live transport, ahead of M1.5's fault-injection provider that will stress it
  harder — a useful early signal that the contract holds.
- SSH lands purely as a provider transport implementation + a secret reference in the
  profile; the Connect/Debug ports and typed provider runtime are untouched, a concrete test of
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
  duplicate the DebugSession lifecycle and bypass the typed provider runtime. SSH is a
  Connect transport like gdbstub; reusing the seam is the point.
- **Run live introspection over the gdbstub instead of SSH.** Rejected: gdbstub is
  stop-the-world stub debugging; drgn live introspection wants a running kernel it can
  read via `/proc/kcore`-style access over a shell, which is the SSH path v1 already
  implements. They are complementary, not substitutes.
- **Defer live `introspect.run` past M1.** Rejected by the M1 scope decision: M1 sweeps
  the M0 debug-plane deferral so the local-libvirt provider is feature-complete before
  M1.5 starts injecting faults into it.
