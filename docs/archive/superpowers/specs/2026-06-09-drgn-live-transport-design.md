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
| provider (not gated) | `providers/local_libvirt/lifecycle/connect.py` | accept the `drgn-live` **kind**; SSH realization + the `ssh://host:port` **handle scheme** unchanged (see "token vs scheme" below) |
| provider (not gated) | `providers/remote_libvirt/connect.py` | add a `drgn-live` **kind** branch returning the bare domain-name handle (ADR-0083 §4); change `close_transport` to a decode-or-no-op so the bare-domain handle does not fail its current unconditional `TransportHandleData.decode` |
| provider (not gated) | `providers/fault_inject/lifecycle/provider.py` | accept the `drgn-live` **kind** (its `_TRANSPORT_KINDS` is the connector accepted-kind set: `ssh`→`drgn-live`); it emits a `drgn-live://` handle, so that scheme must decode (below) |
| ports (not gated) | `providers/ports/lifecycle.py` | `_TRANSPORT_KINDS` here is the **handle-scheme** decode set, NOT the agent-facing token set — it must keep every scheme a connector emits (`gdbstub`, `ssh`) and add `drgn-live` (fault-inject emits it). Do **not** drop `ssh`. |
| gate | `scripts/m2_portability_gate.py` | add the two `mcp/` files (`sessions.py`, `introspect.py`) to `ALLOWED_FILES` |
| docs | `docs/guide/reference/*` | regenerate via `just docs` (tool Field descriptions changed) |

### Transport token vs handle scheme (two distinct vocabularies)

These were conflated in an earlier draft; they are separate and must not be unified:

1. **Transport token** — the agent-facing `transport=` argument, the `debug_sessions.transport`
   column value, the `kind` passed to `Connector.open_transport`, and the per-transport
   single-attach conflict key. This set becomes `{gdbstub, drgn-live}` (owned by
   `sessions.py _TRANSPORTS`, the `introspect.run` gate, and each connector's accepted-kind
   check, including `fault_inject/lifecycle/provider.py _TRANSPORT_KINDS`).
2. **Handle scheme** — `TransportHandleData.kind`, the `<scheme>://host:port` prefix a connector
   encodes into the opaque handle, validated on decode against
   `providers/ports/lifecycle.py _TRANSPORT_KINDS`. Both `close_transport` implementations (local
   and remote) and the gdb-MI `get_or_attach` call `TransportHandleData.decode`, so the decode set
   must accept **every scheme any connector emits**: `gdbstub` (all), `ssh` (local's drgn-live
   realization handle), and `drgn-live` (fault-inject, which passes its kind through into the
   handle). The **remote drgn-live handle is the bare domain name** (ADR-0083 §4) — unschemed — so
   the remote `close_transport` must decode-or-no-op rather than decode unconditionally.

Concretely: `ports/lifecycle.py _TRANSPORT_KINDS = {"gdbstub", "ssh", "drgn-live"}` (the decode
set), while `sessions.py _TRANSPORTS = {"gdbstub", "drgn-live"}` (the token set). A plan task must
verify local and fault-inject `end_session` still round-trips (their handles decode) and remote
`end_session` no-ops cleanly on the bare-domain handle.

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

**Remote `start_session` does no guest-agent reachability probe.** Remote `open_transport("drgn-live")`
returns the domain handle with no IO (unlike local, which probes SSH reachability, and unlike the
gdbstub path, which probes RSP). So a remote `drgn-live` session reaches `live` even if the guest
agent is unreachable; that failure surfaces at `introspect.run` (`transport_failure`). `live` here
means "session row open", not "guest reachable". The reconciler's stale-heartbeat sweep
(`reconciler/loop.py` `_repair_dead_sessions`) is the backstop that detaches an abandoned session.

## Error contract (unchanged taxonomy)

- unknown transport → `configuration_error`
- local-section profile missing `ssh_credential_ref` → `configuration_error {reason: ssh_credential_ref_missing}` (preserved)
- unreachable guest agent → `transport_failure`; non-zero in-guest helper → `debug_attach_failure`; off-gate → `missing_dependency`
- `introspect.run` on a `gdbstub` session → `configuration_error`
- **Cross-tier misuse (pre-existing, documented, not newly guarded):** a gdb-MI op
  (`debug.set_breakpoint`/`read_memory`/…) invoked on a `drgn-live` session is agent misuse. The
  gdb-MI op gate (`mcp/tools/debug/ops.py` `_live_session`) checks state, not transport, so the op
  reaches `get_or_attach` → `TransportHandleData.decode`. Post-rename that decode yields a clean
  `configuration_error` for remote (bare domain, no scheme) and fault-inject, and the same
  pre-existing wrong-endpoint attach for local (`ssh://…`). A `transport == "gdbstub"` guard would
  be cleaner but lives in a third core file (`ops.py`) and would expand the gated surface beyond
  this issue's `sessions.py`+`introspect.py` scope; left as a noted follow-up, not added here.

## Testing

- **Core unit (`tests/mcp/debug/`)** — migrate the existing `ssh` suite to `drgn-live` (credential
  ordering, per-transport conflict, gdbstub/drgn-live coexistence). **Add:** a remote-section
  profile starts a `drgn-live` session with **no** credential resolution, the stored
  `transport_handle` equals the domain name, and `introspect.run` routes to the injected live
  introspector and returns the redacted section. **Add:** a local-section profile missing its
  reference still fast-fails `ssh_credential_ref_missing`.
- **Provider unit (`tests/providers/`)** — remote `open_transport("drgn-live")` returns the
  bare domain-name handle; remote `close_transport` no-ops on that bare-domain handle (decode-or-no-op);
  local `open_transport("drgn-live")` returns its `ssh://` handle and `close_transport` round-trips
  it (decode succeeds); fault-inject `open_transport("drgn-live")` + `close_transport` round-trips;
  unknown kind rejected on every connector.
- **end_session round-trip** — `debug.end_session` on a `drgn-live` session succeeds for local
  (handle decodes), remote (bare domain no-ops), and fault-inject (handle decodes) — the regression
  the handle-scheme/token separation exists to prevent.
- **Profiles unit** — `drgn_live_requires_credential` is `True` for local, `False` for
  remote/fault-inject.
- **Gating untouched** — `live_vm` / `live_stack` stay gated; only unit coverage widens. The remote
  e2e is not modified here.

## Out of scope

- The persistent custom-protocol in-guest agent (recorded as a future direction in ADR-0085).
- Agent-direct-SSH to the target (rejected in ADR-0085).
- The dead-worker gdbstub reconciler reset (#216, separate follow-up).
