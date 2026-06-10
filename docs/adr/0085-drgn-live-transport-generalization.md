# ADR 0085 — Generalize the live-drgn transport off the ssh model (`drgn-live`) (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Amends (does not supersede):** [ADR-0039](0039-ssh-transport-live-introspection.md) (the
  local SSH live-drgn transport this renames to a capability token),
  [ADR-0079](0079-remote-live-debug-transport.md) (the remote live-debug transport design that
  routes in-guest drgn through the guest agent), [ADR-0083](0083-remote-connect-debug-plane.md)
  (which pinned the domain-carrying handle contract and deferred this MCP routing).
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  portability gate this change deliberately extends), [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)
  (the DebugSession lifecycle), [ADR-0078](0078-object-store-in-target-install-seam.md) (the
  guest-agent seam drgn-live rides).
- **Spec:** [`../superpowers/specs/2026-06-09-drgn-live-transport-design.md`](../superpowers/specs/2026-06-09-drgn-live-transport-design.md)
- **Issue:** #215

## Context

The remote in-guest drgn port (`RemoteLiveIntrospect`) is implemented and wired as the remote
runtime's `live_introspector` (#205), but it is not reachable end-to-end. The live-drgn MCP path
is hard-wired to the local SSH model inside the provider-agnostic core (`src/kdive/mcp/`):

- `debug.start_session(transport="ssh")` resolves an `ssh_credential_ref` from the System
  profile. A remote disk-image System (`RemoteLibvirtProfile`) carries no such reference by
  design, so the call fails `configuration_error {reason: ssh_credential_ref_missing}`.
- `introspect.run` gates on `session.transport == "ssh"`.

Both files are inside the ADR-0076 portability-gate core surface, so generalizing them is a
deliberate, separately-reviewed core change — not provider work — which ADR-0083 §Consequences
deferred to this follow-up. The follow-up carries the gate-allowlist extension and this ADR.

The deeper question is *what to name the transport*. The current token `ssh` names a **mechanism**
that is true only for local-libvirt. Across the roadmap the same **live-drgn capability** is
realized by different channels: SSH (local, ADR-0039), qemu-guest-agent exec (remote, ADR-0079),
and plausibly an SSH/agent-exec on cloud (M3) or an in-guest agent over serial/SoL on bare metal
(M4) and PowerVM (M5). The architecture already accepts this split for the gdb-MI tier: ADR-0079
states bare metal "later swaps the gdbstub for KGDB-over-SoL **behind the same Connect port**." A
transport kind therefore names a **tier/capability the provider realizes**, not a wire mechanism.

## Decision

### 1. The live-drgn transport is a capability token, `drgn-live`

Rename the live-introspection transport value from `ssh` to **`drgn-live`** across the agent-facing
surface and the provider connectors. The agent always calls `debug.start_session(run_id,
transport="drgn-live")` regardless of provider; the provider realizes it (local over SSH, remote
over the guest agent). `introspect.run` gates on `session.transport == "drgn-live"`. The
`debug_sessions.transport` column has no CHECK constraint (application-layer `frozenset` only), so
no schema migration is required.

The token names the tier its only current consumer (`introspect.run`) exercises — live drgn
introspection. The mechanism (`ssh://…` handle for local, bare domain name for remote) stays a
provider-internal detail of the handle, which core treats as opaque.

### 2. Credential resolution is profile-derived, not transport-string-derived

Core no longer assumes a live-drgn session needs an SSH credential. A new `profiles/` predicate
`drgn_live_requires_credential(profile)` returns `True` when the profile carries a local-libvirt
section (SSH-realized) and `False` otherwise (remote guest-agent, fault-inject). `start_session`
resolves and registers the SSH credential — preserving the ADR-0039 §2 ordering that seeds the
redaction registry before any transport output — **iff** the predicate is `True`. The
provider-awareness lives in `profiles/` (beside `ssh_credential_ref` / `capture_method` /
`destructive_opt_in`), never in `mcp/`.

This preserves the fast, specific `ssh_credential_ref_missing` configuration error for a
misconfigured local System (predicate `True`, reference absent) while letting a remote System
start a `drgn-live` session with no credential path.

### 3. The remote connector returns the pinned domain-carrying handle

`RemoteLibvirtConnect.open_transport(system, "drgn-live")` returns
`TransportHandle(str(system_handle))` — the bare guest domain name that core's `_open_transport`
derives (`system.domain_name or str(system.id)`), exactly the ADR-0083 §4 input contract
`RemoteLiveIntrospect.introspect_live` already asserts. `close_transport` tolerates the bare-domain
handle and no-ops: the guest-agent channel is connectionless, opened per operation by the port over
`qemu+tls://`. The local connector's `drgn-live` branch is its existing SSH realization unchanged
(the loopback-SSH reachability probe and the `ssh://host:port` handle).

### 4. No new error categories, no new MCP tools

The routing reuses the existing tool surface and the ADR-0079 error mapping: unreachable guest
agent → `transport_failure`; non-zero in-guest helper → `debug_attach_failure`; off-gate drgn →
`missing_dependency`; unknown transport or missing-but-required credential → `configuration_error`.

### 5. The realization stays the existing one-shot constrained helper

This change ships the routing plus the **existing** one-shot, allowlisted in-guest drgn helper
(`/usr/local/sbin/kdive-drgn <helper>`, the three fixed helpers `tasks`/`modules`/`sysinfo`). The
capability naming in Decision 1 is chosen so the realization can later evolve per provider without
re-touching the agent surface or the core routing (see Consequences and the recorded future
direction).

## Consequences

- **Remote in-guest drgn becomes reachable end-to-end** through `debug.start_session` +
  `introspect.run`, the prerequisite for the issue-8 operator e2e to exercise it. The e2e is not
  un-gated by this change; it may later select `transport="drgn-live"`.
- **The portability gate's `ALLOWED_FILES` gains two core files** (`mcp/tools/debug/sessions.py`,
  `mcp/tools/debug/introspect.py`), extended in the same PR — the deliberate, reviewed core
  crossing ADR-0076 requires, recorded here.
- **The generated tool reference changes** (`debug.start_session`'s `transport` description and
  `introspect.run`'s session description move from `ssh` to `drgn-live`); it is regenerated and
  committed (ADR-0047, `just docs`).
- **Local and remote share one agent-facing call.** An agent never needs to know whether a System
  is SSH-reachable or guest-agent-reachable; portability (ADR-0063/0076) holds at the call site,
  not only inside the providers.
- **The local `ssh` token is retired.** ADR-0039's mechanism (drgn over SSH) is unchanged; only the
  transport *name* is generalized. The `ssh_credential_ref` profile field keeps its name — it is an
  SSH credential — and is consulted through the new predicate.

## Considered & rejected

- **Keep `ssh` for local, add a second token `drgn-live` for remote (two live tokens).** Smaller
  diff, but the agent must match transport name to provider, leaking provider knowledge into the
  call site and denting the M2 portability thesis. Rejected.
- **Overload `ssh` to also cover the remote guest-agent path (no rename), relaxing the credential
  check.** Smallest diff, but names a non-SSH channel `ssh` and reintroduces an `ssh` transport on
  the remote surface that ADR-0079 explicitly rejected ("drgn-live over SSH … Rejected"). Rejected.
- **Presence-based credential resolution** (resolve iff an `ssh_credential_ref` is present). Simpler
  than the predicate, but a local System that omits its reference loses the fast, specific
  `ssh_credential_ref_missing` error and instead fails later as a transport/attach error — a
  regression in fail-fast specificity (top-level design §Error taxonomy). Rejected in favor of the
  profile predicate.
- **Simple SSH from the agent directly into the debug target.** This is the single-user PoC model.
  It collapses four production invariants at once: the redaction boundary (raw guest output would
  reach the agent without passing the redactor), the constrained-debug allowlist (arbitrary
  in-guest execution), secret-by-reference (a guest credential would have to reach the requester),
  and routability (in the hosted model the agent is remote over MCP/HTTP while targets sit on a
  worker-reachable segment — bare-metal/PowerVM/private-VPC targets are not agent-routable at all).
  Rejected as a different, single-user product, not a smaller version of this design.

## Recorded future direction (not built here)

The base image already runs a kdive-supplied in-guest helper. A persistent in-guest **kdive agent**
speaking a framed protocol — rather than one-shot exec per call — is an attractive future
realization of the `drgn-live` capability: a warm drgn `Program` held open across queries (drgn's
multi-second symbol/DWARF load is paid once, a large win for the "many Runs against one System"
loop), a richer *typed*, still-allowlisted operation surface returning pre-redacted results, and a
transport-agnostic framing that rides any provider byte pipe (guest-agent exec, virtio-vsock, SSH,
serial/SoL). It is its own milestone-sized effort (shipping and versioning a persistent guest
daemon, channel authn, a larger in-guest attack surface) and is out of scope for #215. The
`drgn-live` capability token is chosen specifically so this evolution needs no change to the
agent-facing transport surface or the core routing.
