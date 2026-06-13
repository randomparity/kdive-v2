# ADR 0103 — Reconciler reachability probe for SSH build hosts

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0099](0099-remote-build-host-targets.md)
  (the `build_hosts` inventory, the `state` column with its `ready`/`unreachable`
  CHECK, the selection seam that already fails closed on `state='unreachable'`, and the
  `SshBuildTransport` + materialized-identity credential pattern this probe reuses),
  [ADR-0086](0086-dead-worker-gdbstub-reset.md) (the reconciler→provider port pattern —
  a narrow protocol the reconciler calls without importing a provider), and
  [ADR-0095](0095-reconciler-remote-console-collector.md) (the optional `| None`
  reconciler port wired only when its provider support is present).
- **Spec:** [`../superpowers/specs/2026-06-13-build-host-reachability-probe.md`](../superpowers/specs/2026-06-13-build-host-reachability-probe.md)
- **Issue:** [#359](https://github.com/randomparity/kdive/issues/359)

## Context

ADR-0099 added a `state text` column to `build_hosts` (CHECK `state IN ('ready',
'unreachable')`, default `'ready'`) and made build-host selection fail closed: a host
whose `state='unreachable'` is rejected at the `runs.build` boundary with
`configuration_error`. But **nothing ever sets `state='unreachable'`**. The column was
introduced for reconciler-owned health that ADR-0099 explicitly deferred.

Today a dead SSH builder is only discovered reactively: a Run is admitted to it, the
build fails (ssh connect error → `build_failure`/`infrastructure_failure`), and the
reconciler's lease reclaim later frees the slot. Every Run routed at a down host pays a
full failed-build cost before the slot is freed, and a host stuck unreachable keeps
attracting and failing builds.

A periodic health probe that flips `state` proactively lets selection skip a dead builder
*before* admitting a Run to it, and flips it back to `ready` when the host recovers — no
operator action required.

## Decision

Add a reconciler repair, `probe_build_host_reachability`, driven by an **optional**
`BuildHostProber` port (the `| None` pattern of ADR-0095, not a Null-object default).

**Port.** `BuildHostProber` is a `Protocol` with one method,
`async def probe(host: BuildHost) -> bool` (True = reachable). The concrete
`SshBuildHostProber` holds the long-lived `SecretRegistry` and, per probe, materializes
the host's SSH identity and runs a bare `ssh <host> true`.

**Reachability primitive.** The probe runs `ssh … true` with **no `cd` into the
workspace** (a short, bounded `ConnectTimeout` + subprocess timeout). Reusing the normal
`BuildTransport.run`, which prefixes `cd <workspace_root> &&`, would conflate "host
unreachable" with "workspace dir absent"; a reachability check must test only the SSH
hop. A non-zero exit, a timeout, an `ssh` launch failure, or a credential-resolution
error all map to **not reachable** (fail-closed — a host we cannot reach or whose
credential we cannot resolve cannot build).

**State transition.** For each `kind='ssh' AND enabled=true` host the repair probes and
writes the resulting state with a compare-and-swap
(`UPDATE … SET state=:new WHERE id=:id AND state=:old`), so the repair only writes (and
only counts) genuine transitions and never clobbers a concurrent operator change. The
count of transitions is reported on `ReconcileReport.build_hosts_probed` for
observability.

**Secret-registry scope.** The reconciler's `SecretRegistry` lives for the process; its
global scope is never evicted (ADR-0012). Registering the SSH key value under the global
scope on every 30s pass would grow the registry unboundedly. Each probe therefore
registers its credential under a **per-probe scope object and releases it** in a
`finally`, so probing is steady-state in memory.

**Wiring.** `SshBuildHostProber` is wired unconditionally in the reconciler entrypoint —
SSH build hosts are independent of the remote-libvirt provider, so the prober is **not**
gated on `_remote_libvirt_enabled`. When no SSH hosts are registered the repair's query
returns nothing and the pass is a no-op.

**Isolation & best-effort.** Probing is sequential and each per-host probe is guarded so
one host's failure neither raises out of the repair nor stops the others. Per-host errors
are logged and treated as `unreachable`. Probes are slow (network) and run **outside any
open transaction**: the host list is read in a committed transaction first, then each
CAS write commits in its own short transaction, so no probe holds a transaction open
across network I/O.

## Consequences

- Selection skips a down SSH builder proactively instead of after a failed build; a
  recovered host returns to `ready` within one reconciler interval.
- One more repair per pass. With the expected small host count the sequential
  `ConnectTimeout`-bounded probes fit comfortably inside the 30s interval; the reconciler
  already tolerates an occasional over-interval pass via its lag telemetry, so a transient
  slow probe degrades cadence rather than correctness.
- `state` becomes reconciler-owned. The `build_hosts.disable` / `enable` operator surface
  controls `enabled`, which is orthogonal to `state`; the probe never touches `enabled`
  and only probes `enabled=true` hosts.
- A misconfigured credential flips the host `unreachable`, surfacing the misconfiguration
  as an unavailable host rather than as cryptic per-build ssh errors.

## Considered & rejected

- **Null-object prober always in the plan** (the ADR-0086 `TransportResetter` pattern).
  Rejected: there is no meaningful no-op reachability result — a Null prober would either
  flip every host `unreachable` or leave `state` permanently stale. The `| None`
  conditional-append pattern (ADR-0095 console hosting) fits a port that is either present
  or absent.
- **Reuse `transport.run(["true"], cwd=workspace_root)`.** Rejected: the `cd
  <workspace_root>` prefix makes an absent/uncreated workspace read as unreachable.
- **Gate the prober behind `_remote_libvirt_enabled`.** Rejected: SSH build hosts
  (ADR-0099) exist independent of the remote-libvirt provider; gating would leave the most
  common SSH-builder deployment unprobed.
- **Probe disabled hosts.** Rejected: a disabled host is never selected, so probing it
  every pass spends an SSH connection for no behavioral effect; it is re-probed the pass
  after it is re-enabled.
- **Concurrent probing (`asyncio.gather`).** Deferred: sequential probing is simpler and
  deterministic, and the host count is small. A future ADR can parallelize if a
  deployment registers many SSH builders.
- **Age/health-history tracking (flap damping, consecutive-failure thresholds).**
  Deferred: the immediate need is a binary reachable/unreachable flip. Hysteresis can be
  layered on later without changing the port.
