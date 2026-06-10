# Design — generalize the live-drgn MCP routing to the `drgn-live` transport (#215)

- **Date:** 2026-06-09
- **Issue:** #215 (M2 follow-up to #205; sibling of #216)
- **ADR:** [0085](../../adr/0085-drgn-live-transport-generalization.md) (amends ADR-0039/0079/0083)
- **Status:** approved for planning

## Problem

`RemoteLiveIntrospect.introspect_live` (in-guest drgn over the qemu-guest-agent seam) is built and
wired as the remote runtime's `live_introspector` (#205), but it is unreachable end-to-end. The
live-drgn MCP path in the provider-agnostic core (`src/kdive/mcp/`) is hard-wired to the local SSH
model:

- `debug.start_session(transport="ssh")` resolves an `ssh_credential_ref` from the System profile;
  a `RemoteLibvirtProfile` carries none, so the call fails
  `configuration_error {reason: ssh_credential_ref_missing}`.
- `introspect.run` gates on `session.transport == "ssh"`.

Both files are inside the ADR-0076 portability-gate core surface, so this is a deliberate,
gate-allowlisted core change with its own ADR — the follow-up ADR-0083 §Consequences deferred.

## Goal / acceptance

- An operator runs in-guest drgn (`tasks` / `modules` / `sysinfo`) against a live **remote** System
  through the MCP tools and gets a constrained, redacted result — with no SSH credential.
- The local SSH live-drgn path keeps working unchanged.
- The change is gate-allowlisted (the two core files added to `scripts/m2_portability_gate.py`
  `ALLOWED_FILES`) in the same PR, with ADR-0085.
- No new `ErrorCategory`, no new MCP tool, no schema migration.

## Decision summary (full rationale in ADR-0085)

1. **Capability token `drgn-live`** replaces the mechanism token `ssh` as the live-introspection
   transport. The agent always calls `start_session(transport="drgn-live")`; the provider realizes
   it (local over SSH, remote over guest-agent). `introspect.run` gates on `drgn-live`.
2. **Credential resolution is profile-derived.** A new `profiles/` predicate
   `drgn_live_requires_credential(profile)` is `True` for a local-libvirt section, `False`
   otherwise. Core resolves the SSH credential iff the predicate is `True`, preserving the
   ADR-0039 §2 ordering and the fast `ssh_credential_ref_missing` error for a misconfigured local
   System.
3. **The remote connector returns the pinned domain-carrying handle.**
   `RemoteLibvirtConnect.open_transport(system, "drgn-live")` returns
   `TransportHandle(str(system_handle))` (the bare domain name `_open_transport` derived,
   `system.domain_name or str(system.id)`); `close_transport` no-ops on it. This is the ADR-0083 §4
   contract `RemoteLiveIntrospect` already asserts.

## Components touched

| Layer | File | Change |
|---|---|---|
| core (gate-allowlisted) | `mcp/tools/debug/sessions.py` | `_SSH`→`_DRGN_LIVE = "drgn-live"`; `_TRANSPORTS = {gdbstub, drgn-live}`; credential resolution keyed on the profile predicate, not the transport string |
| core (gate-allowlisted) | `mcp/tools/debug/introspect.py` | gate `transport == "drgn-live"`; `resolve_live_ssh_session` → `resolve_live_drgn_session`; tool Field descriptions `ssh`→`drgn-live` |
| profiles (not gated) | `profiles/provisioning.py` | add `drgn_live_requires_credential(profile) -> bool` |
| provider (not gated) | `providers/local_libvirt/lifecycle/connect.py` | `open_transport` branch matches `drgn-live`; SSH realization + `ssh://host:port` handle unchanged |
| provider (not gated) | `providers/remote_libvirt/connect.py` | add `drgn-live` branch returning the domain-name handle; `close_transport` tolerates a bare-domain handle |
| ports (not gated) | `providers/ports/lifecycle.py`, `providers/fault_inject/lifecycle/provider.py` | `_TRANSPORT_KINDS = {gdbstub, drgn-live}` |
| gate | `scripts/m2_portability_gate.py` | add the two `mcp/` files to `ALLOWED_FILES` |
| docs | `docs/guide/reference/*` | regenerate via `just docs` (tool Field descriptions changed) |

## Data flow (remote happy path)

```
start_session(run_id, transport="drgn-live")
  preconditions: run booted, System ready, drgn-live endpoint free (per-transport single-attach)
  drgn_live_requires_credential(profile)? remote → False → no credential
  _open_transport → connector.open_transport(SystemHandle("<domain>"), "drgn-live")
                    remote → TransportHandle("<domain>")                      # ADR-0083 §4
  insert debug_sessions {transport:"drgn-live", transport_handle:"<domain>", state→live}

introspect.run(session_id, helper="tasks")
  resolve_live_drgn_session: gate transport=="drgn-live", state==live, operator role
  runtime_for_session → RemoteLiveIntrospect
  introspect_live(transport_handle="<domain>", helper="tasks")
    opens own qemu+tls conn, guest-agent execs the one allowlisted helper,
    assemble_report redacts + byte-caps → {report, truncated, transcript_sensitivity:"sensitive"}
```

Local is identical except the predicate is `True` (credential resolved + registered before
`open_transport`) and the stored handle is `ssh://127.0.0.1:22`. Core treats the handle as opaque
in both cases; each provider's connector produces what its own `live_introspector` consumes.

## Error contract (unchanged taxonomy)

- unknown transport → `configuration_error`
- local-section profile missing `ssh_credential_ref` → `configuration_error {reason: ssh_credential_ref_missing}` (preserved)
- unreachable guest agent → `transport_failure`; non-zero in-guest helper → `debug_attach_failure`; off-gate → `missing_dependency`
- `introspect.run` on a `gdbstub` session → `configuration_error`

## Testing

- **Core unit (`tests/mcp/debug/`)** — migrate the existing `ssh` suite to `drgn-live` (credential
  ordering, per-transport conflict, gdbstub/drgn-live coexistence). **Add:** a remote-section
  profile starts a `drgn-live` session with **no** credential resolution, the stored
  `transport_handle` equals the domain name, and `introspect.run` routes to the injected live
  introspector and returns the redacted section. **Add:** a local-section profile missing its
  reference still fast-fails `ssh_credential_ref_missing`.
- **Provider unit (`tests/providers/`)** — remote `open_transport("drgn-live")` returns the
  domain-name handle; `close_transport` no-ops on a bare-domain handle; unknown kind rejected.
- **Profiles unit** — `drgn_live_requires_credential` is `True` for local, `False` for
  remote/fault-inject.
- **Gating untouched** — `live_vm` / `live_stack` stay gated; only unit coverage widens. The remote
  e2e is not modified here.

## Out of scope

- The persistent custom-protocol in-guest agent (recorded as a future direction in ADR-0085).
- Agent-direct-SSH to the target (rejected in ADR-0085).
- The dead-worker gdbstub reconciler reset (#216, separate follow-up).
