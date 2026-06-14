# Reconciler reachability probe for SSH build hosts — design

- **Date:** 2026-06-13
- **Issue:** [#359](https://github.com/randomparity/kdive/issues/359) (`status:needs-design`)
- **ADR:** [ADR-0103](../../adr/0103-build-host-reachability-probe.md)
- **Status:** Approved (design)

## 1. Problem

ADR-0099 gave `build_hosts` a `state` column (`ready` | `unreachable`, default `ready`)
and made selection fail closed on `state='unreachable'`
(`services/runs/build_host_selection.py:resolve_and_admit`). Nothing sets that state. A
dead SSH builder is only found reactively: a Run is admitted, the build fails on the ssh
hop, and the reconciler's `reclaim_orphan_build_host_leases` later frees the slot. Each
Run routed at a down host pays a full failed-build before the slot frees.

## 2. Goal

A periodic reconciler probe that flips an SSH host's `state` `ready ↔ unreachable` so
selection skips a dead builder proactively and re-admits a recovered one — within one
reconciler interval, no operator action.

Non-goals: flap damping / consecutive-failure thresholds; probing non-SSH kinds
(`local` has no address; `ephemeral_libvirt` VMs are created per-build); concurrent
probing.

## 3. Design

### 3.1 The port

`providers/build_host/reachability.py`:

```python
class BuildHostProber(Protocol):
    async def probe(self, host: BuildHost) -> bool: ...   # True == reachable
```

`SshBuildHostProber(secret_registry, *, probe_timeout_s=15)` implements it. `probe`
offloads the blocking ssh to a thread (`asyncio.to_thread`) so it never stalls the
reconciler event loop, mirroring `console_hosting.AsyncioPumpRunner`. The synchronous body:

1. If `host.address` or `host.ssh_credential_ref` is `None` → return `False` (not an SSH
   host; defensive — the repair only passes ssh hosts).
2. Create a fresh per-probe `scope = object()`.
3. `with materialized_ssh_identity(host.ssh_credential_ref, registry, scope=scope) as
   identity:` build `SshBuildTransport(address=host.address, identity_path=identity,
   secret_registry=registry)` and call `transport.check_reachable(timeout_s=probe_timeout_s)`.
4. Catch `CategorizedError` (credential resolve / identity materialization failure) →
   return `False`.
5. `finally: registry.release(scope)` — evict the per-probe credential so the long-lived
   reconciler registry does not grow each pass (see §4.2).

### 3.2 The reachability primitive

New method on `SshBuildTransport`:

```python
def check_reachable(self, *, timeout_s: int) -> bool:
    """Run a bare `ssh <host> true` (no workspace cd); True iff it exits 0."""
```

It builds `self._ssh_argv("true")` (so it reuses `-i <identity>`, `BatchMode=yes`,
`StrictHostKeyChecking=accept-new`, `ConnectTimeout=10`) and runs it with
`subprocess.run(..., timeout=timeout_s)`. `subprocess.TimeoutExpired` and `OSError`
(launch failure) both return `False`; otherwise return `proc.returncode == 0`. It does
**not** prefix `cd <cwd>` the way `_run_remote` does — a reachability check tests the SSH
hop only, not workspace existence.

`probe_timeout_s` (the `subprocess.run` timeout) is deliberately **larger than ssh's own
`ConnectTimeout=10`** (default 15s) so ssh's connect timeout is the binding failure signal
and the Python timeout is only a backstop against a wedged ssh process; an equal-or-smaller
subprocess timeout would race ssh's still-dialing connect and report a reachable-but-slow
host as down.

On a **non-zero exit** (and on `TimeoutExpired`), `check_reachable` logs the redacted
`stderr` tail (`redacted_tail(proc.stderr, self._secret_registry)`, the helper
`_run_remote` already uses) at `warning` before returning `False`. This is the only
diagnostic a stuck-`unreachable` host gets — in particular a rebuilt builder whose host key
changed fails `accept-new` with `REMOTE HOST IDENTIFICATION HAS CHANGED`, which would
otherwise be silently swallowed (the method returns only a bool). See §4.6 for the
known_hosts ownership assumption.

### 3.3 The repair

`reconciler/build_hosts.py:probe_build_host_reachability(conn, prober) -> int`:

1. In a committed read transaction, select the probe set
   (`db.build_hosts.list_probeable_ssh_hosts`: `kind='ssh' AND enabled=true ORDER BY
   name`). The read commits before any probe so no transaction is held open across
   network I/O.
2. For each host: `reachable = await prober.probe(host)`;
   `new_state = 'ready' if reachable else 'unreachable'`.
3. If `new_state != host.state`, CAS-write it
   (`db.build_hosts.mark_state(conn, host.id, new_state=…, expected_state=host.state)` →
   `UPDATE … SET state=%s WHERE id=%s AND state=%s`, each in its own committed
   transaction) and add its `rowcount` to the transition count.
4. Wrap each host iteration in `try/except` so one host's unexpected failure is logged and
   skipped, never aborting the pass. Return the transition count.

The repair returns the count of state **transitions**, not the number of hosts probed —
so a healthy steady state returns `0`. On a non-empty pass it also logs at `info` the
probed-vs-flipped counts (e.g. ``"probed %d ssh build host(s); %d state change(s)"``) so
"the probe ran" is observable independently of whether any host flipped (the transition
count alone reads `0` exactly when everything is healthy).

### 3.4 DB helpers (`db/build_hosts.py`)

- `list_probeable_ssh_hosts(conn) -> list[BuildHost]`
- `mark_state(conn, host_id, *, new_state, expected_state) -> int` (returns rowcount)

### 3.5 Wiring

- `ReconcileConfig` gains `build_host_prober: BuildHostProber | None = None`.
- `_repair_plan` appends `_RepairSpec("build_host_states_changed", lambda conn:
  probe_build_host_reachability(conn, config.build_host_prober))` **iff**
  `config.build_host_prober is not None`. Placed after `reclaimed_build_host_leases`
  (independent of the reap/reclaim ordering; it neither frees nor consumes capacity).
- `ReconcileReport` gains `build_host_states_changed: int = 0`; `reconcile_once` reports
  `counts.get("build_host_states_changed", 0)`. The name states the count semantics
  (transitions written), matching the `reclaimed_*`/`reaped_*` "count of changes"
  convention — not a probe count, which would read `0` in healthy steady state.
- `loop.__all__` gains `_probe_build_host_reachability` alias (loop-module export
  convention; covered by a registration test like `test_reclaim_spec_registered_in_loop`).
- `ProviderComposition.build_reconciler_build_host_prober() -> BuildHostProber` returns
  `SshBuildHostProber(secret_registry=self._secret_registry)` unconditionally.
- `__main__._run_reconciler` passes `build_host_prober=
  provider_composition.build_reconciler_build_host_prober()` into `ReconcileConfig`.

## 4. Edge cases & invariants

### 4.1 Fail-closed
Unreachable, timeout, ssh launch error, and credential-resolution error all → `False` →
`unreachable`. Selection already rejects `unreachable` (`configuration_error`).

### 4.2 No registry growth
The reconciler `SecretRegistry` is process-lifetime and its global scope is never evicted.
Per-probe `scope` + `release(scope)` keeps registration steady-state across passes. This
is the one behavior a long-lived periodic caller must add over the one-shot build path
(`SshBuildTransport.from_host`, which registers globally and relies on the worker clearing
the registry per op).

### 4.3 CAS, no clobber
`mark_state` only updates the row when `state` still equals what the probe observed, so a
concurrent operator action (or the benign single-reconciler re-run) cannot be clobbered
and a no-op probe writes/counts nothing.

### 4.4 No transaction across I/O
Host list read commits first; each CAS write is its own committed transaction; probes run
between, touching no DB connection. No idle-in-transaction across the ssh round trip.

### 4.5 Disabled / non-ssh hosts untouched
Only `kind='ssh' AND enabled=true` rows are probed. `enabled` is operator-owned and
orthogonal; the probe never writes it. A host re-enabled after repair keeps its stale
`state` until the next probe (≤ one interval), during which selection may still reject it;
this self-corrects and is acceptable for a low-priority health signal.

### 4.6 Busy-host false-negative and known_hosts ownership
The probe is binary and ADR-0103 deferred hysteresis, so a **healthy but saturated** host
(an in-flight `make` starving CPU/IO) whose `ssh … true` exceeds `probe_timeout_s` flips to
`unreachable` for one interval. Only **new** admissions are affected — an in-flight build
keeps running, and the host returns to `ready` the pass after load drops. If this window
ever proves unacceptable in practice it is the trigger for the deferred
consecutive-failure threshold, not a redesign of the port.

The probe runs as the **reconciler process user**, so `accept-new` writes/reads that user's
`known_hosts`. A reimaged builder (changed host key) therefore stays `unreachable` until
that `known_hosts` entry is cleared; the redacted-stderr `warning` (§3.2) is what makes
this diagnosable rather than an opaque dead host. Managing/rotating the reconciler's
`known_hosts` is an operator/deployment concern, out of scope here, but explicitly named so
it is not assumed away.

## 5. Tests (behavior, at the seam)

Reconciler repair (real pool, fake prober), in `tests/reconciler/test_build_hosts.py`:

- ready host probes reachable → no transition, count 0.
- ready host probes unreachable → flips to `unreachable`, count 1.
- unreachable host probes reachable → flips to `ready`, count 1.
- disabled ssh host is not probed (fake prober records no call; state unchanged).
- `local` host is not probed.
- one host raising in the fake prober does not stop a second host from flipping.
- `reconcile_once` reports `build_host_states_changed` (count of transitions, `0` when no
  host flips); the repair is absent from the plan when `build_host_prober is None` and
  present when set; `_probe_build_host_reachability` is in `loop.__all__`.

Prober + primitive (no real network), in `tests/providers/build_host/`:

- `SshBuildHostProber.probe` returns `True`/`False` from a stubbed `check_reachable`.
- No registry growth: after N probes against a real `SecretRegistry`,
  `registry.snapshot()` equals the pre-probe baseline (the refcount keyset returns to
  empty/baseline once each per-probe `scope` is released). **Do not assert on
  `version()`** — it increments on every `register` *and* `release`, so it grows by design
  and is not a leak signal; `snapshot()`/refcount is the measurable steady-state. The
  per-probe scope must be non-`None` (a `None` scope is never evicted by `release`).
- credential-resolution `CategorizedError` → `probe` returns `False` (not raised).
- `SshBuildTransport.check_reachable`: argv contains `true` and no `cd`; `returncode==0` →
  `True`; non-zero / `TimeoutExpired` / `OSError` → `False` (stub `subprocess.run`). On a
  non-zero exit the redacted `stderr` tail is logged at `warning` (assert via `caplog`),
  and a registered secret value in that stderr does not appear in the log record.

DB helpers, in `tests/db/test_build_hosts_repo.py`:

- `list_probeable_ssh_hosts` returns only `kind='ssh' AND enabled=true`.
- `mark_state` CAS: matching `expected_state` → rowcount 1 and row changed; mismatched →
  rowcount 0 and row unchanged.

## 6. Guardrails

`just lint`, `just type`, `just test` (CI runs each recipe separately). Doc changes also
pass `just check-mermaid` and the prose-style guard.
