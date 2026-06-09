# ADR 0083 — Remote connect/debug plane: shared gdb-MI/drgn infra + ACL'd direct-TCP gdbstub (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0079](0079-remote-live-debug-transport.md) (the
  remote live-debug transport design this implements), [ADR-0076](0076-remote-libvirt-provider-package.md)
  (the independent remote-libvirt package + portability gate), [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the guest-agent in-target seam drgn-live reuses), [ADR-0080](0080-remote-provisioning-disk-image-profile.md)
  (the domain-XML gdbstub port registry the connector reads), [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)
  (the gdbstub Connect plane + DebugSession lifecycle), [ADR-0034](0034-debug-plane-gdbmi-tier.md)
  (the gdb-MI tier), [ADR-0033](0033-drgn-introspection-from-vmcore.md) (vmcore drgn),
  [ADR-0039](0039-ssh-transport-live-introspection.md) (the local SSH live-drgn path the remote
  guest-agent path replaces).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md) §Decomposition issue 6
- **Issue:** #205

## Context

ADR-0079 settled *what must cross the network* for remote live debug. This ADR settles *where
the implementing code lives* and *how the remote provider reuses the worker-side debug
mechanics* without coupling to local-libvirt or breaching the ADR-0076 portability gate.

Three concrete obstacles drove the decisions below:

1. **The real gdb-MI engine and the drgn report helpers live inside `local_libvirt/`.** The
   gdb-MI engine (`debug_gdbmi.py`: MI parsing, command timeouts, transcript redaction) and the
   drgn report assembly (`introspect_drgn.py`: the three fixed helpers, redaction, byte-cap) are
   worker-side debug mechanics with nothing libvirt-local about them — yet they sit under
   `providers/local_libvirt/debug/`. Remote needs the *real* engine (its acceptance is real
   gdb-MI over direct TCP, not a mock like fault-inject's), so reuse means either coupling
   remote→local (against ADR-0076's independence goal) or duplicating ~500 lines.

2. **The local connector enforces loopback as an SSRF control; the remote gdbstub is
   deliberately a remote host.** `LocalLibvirtConnect` and `GdbMiEngine.attach` both reject any
   non-loopback RSP host (the ported v1 "F2" control, ADR-0032 §5). On a remote host the gdbstub
   port is on the *remote* host, reached over the operator-ACL'd worker↔host segment (ADR-0079).
   The loopback gate is correct for co-located QEMU and wrong for remote — the reachability
   control moves from "must be loopback" to "the operator ACL restricts the port to the worker
   pool source" (ADR-0079, the network ACL *is* the auth).

3. **The live-drgn MCP path is hard-wired to the local SSH model in core.**
   `debug.start_session(transport="ssh")` resolves an `ssh_credential_ref` from the System
   profile, and `introspect.run` gates on `session.transport == "ssh"`. Both files are under
   `mcp/` — inside the ADR-0076 portability-gate core surface. A remote disk-image System has no
   ssh credential and reaches drgn through the qemu-guest-agent, not ssh, so routing remote
   in-guest drgn through these tools requires generalizing core (a gate-blocked change).

## Decision

### 1. Extract a provider-neutral debug-infra package

Move the worker-side debug mechanics out of `local_libvirt/debug/` and the RSP codec out of
`local_libvirt/lifecycle/connect.py` into a new provider-neutral package
`src/kdive/providers/debug_common/`:

- `gdbmi.py` — the gdb-MI engine, MI records, execution control, controllers.
- `introspect.py` — the three fixed drgn helpers, `assemble_report` (redact-then-byte-cap), and
  the narrow `_Program`/`_Task`/`_Module` protocols.
- `rsp.py` — `rsp_frame` / `valid_rsp_frame` / `rsp_reachable`.

`local_libvirt` re-imports these from `debug_common`; the move is behavior-preserving (the
local provider's own `Connect`, `VmcoreIntrospect`, and `LiveIntrospect` wiring classes stay in
`local_libvirt/`). `debug_common` lives under `providers/` (not a portability-gate core prefix),
so the extraction touches no gated surface. This is what ADR-0076's hypothesis predicts: a new
provider is provider-specific wiring over shared seams, not a copy of them.

### 2. The RSP host reachability is a policy parameter, not a hard-coded loopback gate

The engine's `attach` and the connectors take a **host-reachability policy** — a callable that
validates the resolved RSP host or raises `CONFIGURATION_ERROR`. Local wires the loopback-only
policy (unchanged SSRF control). Remote wires an ACL-remote policy: the host must be a non-empty,
syntactically well-formed host but **need not be loopback**. The local loopback gate is an SSRF
control because local resolves the endpoint from a libvirt domain; the remote host is **not** a
resolved value — it is `RemoteLibvirtConfig.gdb_addr`, the operator-supplied gdbstub listen
address (config.py: "the ACL'd security boundary … must be named explicitly; provisioning fails
closed when unset"). It is operator-trusted config, exactly like the `qemu+tls://` URI, so the
SSRF threat the loopback gate addresses does not apply; the operator ACL restricting the
unauthenticated gdbstub to the worker-pool source is the security boundary (ADR-0079). The
policy is the *only* behavioral difference between the local and remote gdb-MI attach.

### 3. Remote gdbstub connector composes the endpoint from operator config + the domain XML

`RemoteLibvirtConnect.open_transport(system, "gdbstub")` composes the gdbstub endpoint from two
sources: the **host is `RemoteLibvirtConfig.gdb_addr`** (operator config, the bind/ACL address),
and the **per-System port is read from the running domain's definition** over the qemu+tls
connection (the port provisioning allocated from `[gdb_port_min, gdb_port_max]` and recorded in
the domain XML, ADR-0080). It applies the ACL-remote policy, probes RSP reachability with the
shared probe, and returns the same `TransportHandleData`-encoded handle the gdb-MI tier already
consumes. The slow seams (XML port read, socket probe) are injected and `live_vm`-gated;
orchestration and the full error contract are unit-tested with fakes. `close_transport`
validates the handle and no-ops (connectionless RSP). The remote `attach_seam` spawns the
worker's gdb against `gdb_addr:port` with the ACL-remote policy.

### 4. In-guest drgn-live runs through the guest-agent seam, not ssh

`RemoteLiveIntrospect.introspect_live(transport_handle, helper)` validates `helper` against the
fixed in-tree set **worker-side** (never an in-guest shell), composes the constrained drgn
invocation, and runs it inside the guest through the ADR-0078 guest-agent exec seam (the same
seam install uses), reusing the shared `assemble_report` for the single redaction + byte-cap
boundary. The base image carries drgn + matching vmlinux (a provisioning-profile obligation,
ADR-0079).

**Input contract (pinned now so the unit tests assert the real shape).** Remote drgn-live does
not ride the gdbstub transport — it reaches the guest agent keyed by **domain**. So for remote,
`transport_handle` carries the **guest domain name** (the same `SystemHandle` value
`debug.start_session`'s `_open_transport` derives, `system.domain_name or str(system.id)`); the
port resolves the qemu+tls connection from `RemoteLibvirtConfig` and runs the agent exec against
that domain. The port is implemented and unit-tested against this contract in #205 and wired
into the remote runtime's `live_introspector`; the **end-to-end MCP routing is deferred** — the
deferred follow-up's job is to make `start_session`/`introspect.run` open and pass exactly this
domain-carrying handle (see Consequences).

### 5. Worker-side vmcore postmortem reuses the offline drgn path

`RemoteVmcoreIntrospect.from_vmcore` fetches the vmcore + vmlinux from the object store on the
worker, verifies the core's build-id against the Run's recorded build-id, and runs the shared
drgn helpers locally — no live reachability (ADR-0079). Its tool (`introspect.from_vmcore`) is
keyed on `run_id` with no ssh coupling, so the **port + run-keyed tool wiring land in #205**.
The tool resolves the Run's System's *captured* vmcore key, and remote capture is the
still-stubbed Retrieve plane (issue 7 / #206) — so a remote Run has no core to introspect until
#206 lands. In #205 the only signal is unit tests driving the port with a fake-fetched core; an
actual remote vmcore postmortem is exercisable once issue 7 supplies the capture path.

## Consequences

- **gdb-MI direct-TCP lands end-to-end in #205**, exercising the real gdb-MI tier on the remote
  provider (acceptance criterion 1). It is entirely within `providers/` and `tests/providers/`,
  so the portability gate stays green with no allowlist change.
- **Worker-side vmcore postmortem lands as a wired port + run-keyed tool in #205, but is not
  end-to-end-exercisable until issue 7.** The from_vmcore port and `introspect.from_vmcore`
  routing land here; an actual remote core to introspect requires the issue-7 capture plane
  (the Retrieve plane is the still-stubbed `UnimplementedRetriever`). #205's signal is port unit
  tests with a fake-fetched core.
- **In-guest drgn-live is delivered at the port + composition level in #205**, unit-tested
  through the guest-agent seam against the pinned domain-carrying handle contract (§4). It has
  **no end-to-end verification — neither an MCP tool nor the issue-8 e2e — until the deferred
  MCP routing lands** (the e2e drives the spine through `introspect.run`, which is part of the
  deferral). #205's only signal for the in-guest-drgn half of acceptance criterion 2 is the port
  unit tests.
- **Interim limitation: a single-client gdbstub wedged by a dead worker has no automated
  recovery in #205.** ADR-0079's reconciler reset is deferred (below), so between #205 and the
  follow-up a worker that dies mid-debug can leave its stale TCP connection holding the
  System's single-client gdbstub, and the next attach fails (`transport_conflict` /
  `debug_attach_failure`) until the System is torn down and reprovisioned. `close_transport` is
  a no-op (connectionless RSP) and the holding connection belongs to the dead worker, so the
  provider cannot break it without the core reconciler reset.
- **Two pieces are deferred to a follow-up** because they are core coupling the ADR-0076 gate
  deliberately blocks, not provider work:
  - **drgn-live MCP routing** — generalizing `start_session`/`introspect.run` off the
    ssh-transport + ssh-credential assumption so remote guest-agent drgn is reachable through the
    tools. Needs a core change (`mcp/`) + an allowlist extension + its own ADR.
  - **The dead-worker gdbstub reconciler reset** (ADR-0079's single-client-contention
    consequence, `→ transport_conflict`). The reconciler is core (`reconciler/`); the reset is a
    deliberate, separately-reviewed core change. The spec's issue-6 decomposition row and #205's
    acceptance criteria already omit it.

  The follow-up carries the gate-allowlist extension and the ADR amendment for both.
- **The extraction makes local and remote share one tested gdb-MI engine and one drgn report
  assembler.** A future provider (cloud, bare-metal) reuses `debug_common` with its own host
  policy and transport, keeping the ADR-0063 falsifiability claim measurable.
- **No new error strings.** Unreachable gdbstub / guest agent → `transport_failure`; an
  unattachable endpoint → `debug_attach_failure`; a build-id provenance mismatch →
  `configuration_error`; off-gate drgn → `missing_dependency` (all existing categories, ADR-0079).

## Alternatives considered

- **Remote imports local-libvirt's engine directly.** Smallest diff, but couples remote→local
  against ADR-0076's independence goal; a reviewer enforcing the gate's spirit would flag it.
  Rejected.
- **Duplicate the engine + helpers into `remote_libvirt/`.** Satisfies independence literally
  but copies ~500 lines of tested mechanics — two copies to drift. Rejected.
- **Add a non-loopback flag to the local engine in place.** Keeps the engine where it is but
  weakens the local SSRF control's locality and still couples remote→local. Rejected in favor of
  the host-policy parameter on the extracted engine.
- **Extend the portability-gate allowlist now to deliver drgn-live MCP routing + the reconciler
  reset in #205.** Crosses the core boundary the gate protects for two changes that are not
  provider work; declined for #205 and tracked as the deliberate, separately-reviewed follow-up.
