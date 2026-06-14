# Implementation plan — Reconciler reachability probe for SSH build hosts (#359)

- **Spec:** [`../specs/2026-06-13-build-host-reachability-probe.md`](../specs/2026-06-13-build-host-reachability-probe.md)
- **ADR:** [ADR-0103](../../adr/0103-build-host-reachability-probe.md)
- **Branch:** `feat/build-host-reachability-probe-359`

## Conventions (apply to every task)

- Python 3.13, `uv`. Absolute imports only (`kdive.…`), no relative imports. Ruff line
  length 100, lint set `E,F,I,UP,B,SIM`. Google-style docstrings on public APIs.
- TDD: write the failing test first, confirm it fails for the expected reason, then the
  minimal implementation, then re-run focused test + guardrails.
- Guardrails before every commit: `just lint`, `just type`, `just test` (CI runs each
  separately; `just type` is whole-tree src+tests). Doc changes also `just check-mermaid`.
- Run one focused test: `uv run python -m pytest <path>::<name> -q`.
- Conventional-commit subjects ≤72 chars, imperative; end every commit with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Error taxonomy: reuse existing `ErrorCategory` values, never invent strings.
- Redaction: any ssh stderr surfaced to a log goes through `redacted_tail(text,
  secret_registry)` first.
- Tests mirror the package tree under `tests/`. DB/reconciler tests need the disposable
  Postgres fixture (`migrated_url`) and skip when Docker is absent.

Dependency edges (do the tasks in strict numeric order regardless): Tasks 1 and 2 are
independent of each other; Task 3 depends on Task 2 (`check_reachable`); Task 4 depends on
Tasks 1 (db helpers) and 3 (port); Task 5 depends on Tasks 3 and 4; Task 6 depends on
Task 3. Do them in numeric order in one session (sequential subagent dispatch or inline);
do not parallelize mutating work in this single working tree, and do not start a task
before its prerequisites above are merged into the branch.

---

## Task 1 — DB helpers: `list_probeable_ssh_hosts` + `mark_state`

**Where it fits:** the SQL seam the reconciler repair (Task 4) calls. Keeps build_hosts
SQL in `db/build_hosts.py` (module convention) rather than inlined in the reconciler.

**Files:** `src/kdive/db/build_hosts.py`, `tests/db/test_build_hosts_repo.py`.

**Do:**
- Add `async def list_probeable_ssh_hosts(conn) -> list[BuildHost]`:
  `SELECT * FROM build_hosts WHERE kind = 'ssh' AND enabled = true ORDER BY name`, mapping
  each row with the existing `_row_to_host`. Use `conn.cursor(row_factory=dict_row)` like
  `get_by_name`.
- Add `async def mark_state(conn, host_id: UUID, *, new_state: str, expected_state: str)
  -> int`: `UPDATE build_hosts SET state = %s WHERE id = %s AND state = %s`, returning
  `cur.rowcount`. Google-style docstring noting the CAS semantics (writes only when the
  observed state still holds).

**Tests (write first, must fail):**
- `list_probeable_ssh_hosts` returns only `kind='ssh' AND enabled=true` rows: seed an ssh
  host (enabled), a disabled ssh host, and the local `worker-local` seed; assert the result
  is exactly the enabled ssh host. (Seed ssh rows like the existing
  `_seed_ssh_build_host` in `tests/reconciler/test_build_hosts.py`.)
- `mark_state` matching `expected_state` → returns 1 and the row's `state` changed.
- `mark_state` mismatched `expected_state` → returns 0 and the row's `state` unchanged.

**Acceptance:** both helpers exist, typed, docstringed; three tests pass; `just lint type`
clean.

**Rollback:** pure additions; revert the two functions + tests if abandoned.

---

## Task 2 — `SshBuildTransport.check_reachable`

**Where it fits:** the reachability primitive the SSH prober (Task 3) calls. Reuses the
existing ssh argv + identity, runs a bare `ssh … true` with no workspace `cd`.

**Files:** `src/kdive/providers/build_host/ssh_transport.py`,
`tests/providers/build_host/test_ssh_transport.py` (create if absent; check for an existing
ssh-transport test module first and extend it).

**Do:**
- Add `def check_reachable(self, *, timeout_s: int) -> bool` to `SshBuildTransport`.
  Build `ssh_argv = self._ssh_argv("true")` (reuses `-i identity`, `BatchMode=yes`,
  `StrictHostKeyChecking=accept-new`, `ConnectTimeout=10`). Run
  `subprocess.run(ssh_argv, timeout=timeout_s, check=False, capture_output=True,
  text=True)`. On `subprocess.TimeoutExpired` → log a `warning` ("ssh reachability probe
  timed out") and return `False`. On `OSError` → log a `warning` and return `False`. On a
  non-zero `returncode` → log at `warning` the redacted stderr tail
  (`redacted_tail(proc.stderr, self._secret_registry)`) and return `False`. Return
  `proc.returncode == 0` otherwise.
- Google-style docstring: states it does NOT prefix `cd <cwd>` (tests the SSH hop only) and
  that all non-success outcomes return `False`.

**Tests (write first, must fail):** monkeypatch `subprocess.run` (do not hit the network).
- argv passed to `subprocess.run` ends with `"true"` and contains no `cd`/`&&` (assert on
  the captured argv).
- `returncode == 0` → `True`.
- non-zero `returncode` → `False`, and the redacted stderr tail is logged at `warning`
  (`caplog`); a value registered in the `secret_registry` that appears in stderr does NOT
  appear in the log record.
- `subprocess.run` raising `TimeoutExpired` → `False`.
- `subprocess.run` raising `OSError` → `False`.

Construct the transport directly:
`SshBuildTransport(address="10.0.0.1", identity_path=Path("/tmp/x"),
secret_registry=SecretRegistry())` — `_validate_ssh_destination` accepts `10.0.0.1`.

**Acceptance:** method added; five tests pass; no real ssh invoked; `just lint type` clean.

**Rollback:** additive method + tests; revert if abandoned.

---

## Task 3 — `BuildHostProber` port + `SshBuildHostProber`

**Where it fits:** the reconciler→provider port (the seam the repair depends on), with the
concrete SSH implementation. Mirrors `providers/transport_reset.py` (Protocol next to the
concrete impl in the provider layer).

**Files:** `src/kdive/providers/build_host/reachability.py` (new),
`tests/providers/build_host/test_reachability.py` (new).

**Do:**
- Define `class BuildHostProber(Protocol)` with `async def probe(self, host: BuildHost) ->
  bool`. Decorate `@runtime_checkable` (matches `TransportResetter`).
- `class SshBuildHostProber` with
  `__init__(self, *, secret_registry: SecretRegistry, probe_timeout_s: int = 15)`.
  `async def probe(self, host)` → `return await asyncio.to_thread(self._probe_sync, host)`.
  `_probe_sync(self, host) -> bool`:
  1. if `host.address is None or host.ssh_credential_ref is None` → return `False`.
  2. `scope = object()` (a fresh per-probe, non-`None` scope so `release` evicts it).
  3. `try:` with `materialized_ssh_identity(host.ssh_credential_ref, self._secret_registry,
     scope=scope) as identity_path:` construct
     `SshBuildTransport(address=host.address, identity_path=identity_path,
     secret_registry=self._secret_registry)` and
     `return transport.check_reachable(timeout_s=self._probe_timeout_s)`.
  4. `except CategorizedError:` log a `warning` and return `False`.
  5. `finally: self._secret_registry.release(scope)`.
- Import `materialized_ssh_identity` and `SshBuildTransport` from
  `kdive.providers.build_host.ssh_transport`; `BuildHost` from `kdive.db.build_hosts`;
  `SecretRegistry` from `kdive.security.secrets.secret_registry`; `CategorizedError` from
  `kdive.domain.errors`.

**Tests (write first, must fail):** use a real `SecretRegistry`; monkeypatch
`check_reachable` (or `materialized_ssh_identity` + the transport) so no network/fs is hit.
- `probe` returns `True` when the stubbed `check_reachable` returns `True`; `False` when it
  returns `False`.
- no registry growth: run `probe` N (e.g. 5) times against a real `SecretRegistry` with a
  stub that records the scope used; assert `registry.snapshot()` equals the pre-probe
  baseline after the calls (per-probe scope released). Do NOT assert on `version()`.
- a `host` with `ssh_credential_ref=None` → `probe` returns `False` without constructing a
  transport.
- a `CategorizedError` raised from `materialized_ssh_identity` → `probe` returns `False`
  (not raised) and still releases the scope (snapshot baseline restored).

For the registry-growth test, stub `materialized_ssh_identity` so it actually registers a
value under the passed `scope` (mimic the real one) and yields a dummy `Path`, and stub the
transport's `check_reachable` to return a bool — this exercises the real
`register`/`release` path. Build a `BuildHost` with `kind='ssh'`, `address='10.0.0.1'`,
`ssh_credential_ref='cred-ref'`.

**Acceptance:** port + impl exist, typed; four tests pass; no network/fs; `just lint type`
clean.

**Rollback:** new module + tests; delete if abandoned.

---

## Task 4 — Reconciler repair `probe_build_host_reachability`

**Where it fits:** the repair the reconciler loop runs each pass; consumes Task 1 helpers
and a `BuildHostProber` (Task 3 in prod, a fake in tests).

**Files:** `src/kdive/reconciler/build_hosts.py`,
`tests/reconciler/test_build_hosts.py`.

**Do:**
- Add `async def probe_build_host_reachability(conn, prober: BuildHostProber) -> int`:
  1. read the probe set: `async with conn.transaction(): hosts = await
     list_probeable_ssh_hosts(conn)` (commit the read before probing).
  2. `changed = 0`; for each `host`: wrap the body in `try/except Exception` (log + skip;
     `# noqa: BLE001` with a one-host-isolation comment matching the module's style):
     `reachable = await prober.probe(host)`;
     `new_state = "ready" if reachable else "unreachable"`;
     if `new_state != host.state`: `async with conn.transaction(): changed += await
     mark_state(conn, host.id, new_state=new_state, expected_state=host.state)`.
  3. on a non-empty probe set, log at `info` the probed count and `changed` count.
  4. return `changed`.
- Import `BuildHostProber` from `kdive.providers.build_host.reachability` and
  `list_probeable_ssh_hosts`/`mark_state` from `kdive.db.build_hosts`.

**Tests (write first, must fail):** real pool (`migrated_url`), a fake prober.
Add a `_FakeProber` (a dict `{host_id_or_address: bool}` or a callable; record calls).
- ready ssh host probes reachable → returns 0, row stays `ready`.
- ready ssh host probes unreachable → returns 1, row becomes `unreachable`.
- `unreachable` ssh host probes reachable → returns 1, row becomes `ready`. (Seed the host
  with `state='unreachable'`.)
- a disabled ssh host is not probed (fake prober asserts it was never called for that host;
  state unchanged).
- a `local` host (the seed) is not probed.
- with two ssh hosts where the fake prober raises for the first, the second still flips and
  the repair returns 1 (one-host isolation).
- observability: on a non-empty probe set with one host flipping (probed=2, changed=1),
  assert via `caplog` at `INFO` that the repair logs both the probed count and the changed
  count. Assert on the counts, not the exact message string. This guards the probed-vs-
  flipped log (step 3) that makes "the probe ran" observable when no host flips.

Drive the repair through the existing `run_repair(pool, lambda conn:
probe_build_host_reachability(conn, fake))` helper.

**Acceptance:** repair added; six tests pass; `just lint type test` clean for the file.

**Rollback:** additive function + tests.

---

## Task 5 — Wire into the loop: config, plan, report, `__all__`

**Where it fits:** makes the repair run in `reconcile_once` and surfaces its count.

**Files:** `src/kdive/reconciler/loop.py`, `tests/reconciler/test_build_hosts.py`
(plan/report assertions).

**Do:**
- Import `probe_build_host_reachability` and alias
  `_probe_build_host_reachability = build_host_repairs.probe_build_host_reachability`;
  add the alias to `__all__`.
- Import the `BuildHostProber` type for the annotation (`from
  kdive.providers.build_host.reachability import BuildHostProber`); add
  `build_host_prober: BuildHostProber | None = None` to `ReconcileConfig`.
- Add `build_host_states_changed: int = 0` to `ReconcileReport`.
- In `_repair_plan`, after the `reclaimed_build_host_leases` spec, append — guarded by
  `if config.build_host_prober is not None:` — a
  `_RepairSpec("build_host_states_changed", lambda conn:
  _probe_build_host_reachability(conn, prober))` where `prober = config.build_host_prober`
  is bound to a local first (avoid the late-binding closure trap; match how
  `upload_store`/`console_registry` capture a local).
- In `reconcile_once`, set `build_host_states_changed=counts.get(
  "build_host_states_changed", 0)` on the returned report.

**Tests (write first / extend):**
- `_probe_build_host_reachability` is in `loop.__all__` (mirror
  `test_reclaim_spec_registered_in_loop`).
- the repair is absent from `_repair_plan(...)` when `ReconcileConfig()` has no prober, and
  present when `ReconcileConfig(build_host_prober=fake)` is passed (assert on the spec
  names, like `test_build_vm_reap_runs_before_lease_reclaim_in_repair_plan`).
- `reconcile_once(pool, NullReaper(), config=ReconcileConfig(build_host_prober=fake))`
  reports `build_host_states_changed` matching a seeded transition (seed one ssh host
  `state='ready'`, fake returns unreachable → expect `1`).

**Acceptance:** wiring compiles; the loop test file passes; `just type` whole-tree clean
(watch the new import not creating a cycle — `reconciler` already imports from
`providers`, e.g. `transport_reset`, so this is an established direction).

**Rollback:** revert the loop edits; `ReconcileReport`/`ReconcileConfig` field additions
are backward-compatible defaults.

---

## Task 6 — Composition + entrypoint wiring

**Where it fits:** constructs the prober in production and passes it to the reconciler.

**Files:** `src/kdive/providers/composition.py`, `src/kdive/__main__.py`,
`tests/providers/test_composition.py` (or the existing composition test module).

**Do:**
- In `ProviderComposition`, add `def build_reconciler_build_host_prober(self) ->
  BuildHostProber: return SshBuildHostProber(secret_registry=self._secret_registry)`.
  Unconditional — NOT gated on `_remote_libvirt_enabled` (SSH build hosts are independent
  of the remote-libvirt provider). Import `BuildHostProber`/`SshBuildHostProber` from
  `kdive.providers.build_host.reachability`.
- In `__main__._run_reconciler`, add
  `build_host_prober=provider_composition.build_reconciler_build_host_prober()` to the
  `ReconcileConfig(...)` call.

**Tests (write first):**
- `ProviderComposition(secret_registry=SecretRegistry()).build_reconciler_build_host_prober()`
  returns an object that is a `BuildHostProber` (isinstance via `@runtime_checkable`) and an
  `SshBuildHostProber`; it is returned even when remote-libvirt is not configured (assert it
  is non-`None` with no remote env set).

**Acceptance:** composition method + entrypoint wiring; test passes; `just lint type test`
clean. `__main__` is not unit-tested for the run loop, so verify the import/call by
`just type` (whole-tree) rather than a runtime test.

**Rollback:** revert the composition method + the one `__main__` kwarg.

---

## Final verification (before PR)

- `just lint`, `just type`, `just test` all green locally (these are the CI hard gates;
  CI runs them as separate recipes).
- `just check-mermaid` for the doc changes.
- Confirm no test was un-gated and no existing gate widened (the new tests are plain
  service/unit/db tests, not `live_vm`/`live_stack`).
- Grep for any remaining `build_hosts_probed` (old name) — there should be none.
