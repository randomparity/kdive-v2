# Connect plane (gdbstub) + DebugSession lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task — write the failing test first, then the implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the M0 Connect plane for issue #20 — a realized gdbstub transport port (`connect.py`) and two synchronous session-lifecycle tools (`debug.start_session` / `debug.end_session`) that own the `debug_sessions` row from `attach → live → detached`, with single-attach enforced per System.

**Architecture:** Two new modules plus registration. `src/kdive/providers/local_libvirt/connect.py` ports the RSP-framing helpers (`rsp_frame`/`valid_rsp_frame`) and bounded-IO probe from v1 and exposes a seam-injected, `live_vm`-gated `Connector` (`open_transport`/`close_transport`). `src/kdive/mcp/tools/debug.py` holds the two plain async tool handlers (pool + ctx injected, tested directly), wrapped by FastMCP tools in `register(app, pool)`. `mcp/app.py` gains one `_PLANE_REGISTRARS` append (no handler registrar — the tools are synchronous, no `JobKind`). No schema migration.

**Concurrency model (load-bearing):** `start_session` does a lockless pre-read (fast-fail), opens the transport **outside** any DB lock/transaction (the probe is multi-second network IO), then takes the per-System advisory lock to **re-check single-attach + System-ready authoritatively** and insert+transition in one transaction; on a lost race it closes the just-opened transport and bails. `end_session` resolves the System id via a `debug_sessions → runs` join before taking the lock, then transitions the row `→ detached` under it. The `force_crash`/reboot `live → detached` path is **#23's** (`control.py` `_detach_sessions`) and is not re-implemented here.

**Tech Stack:** Python 3.13, stdlib for the transport (`ipaddress`, `socket`, `threading`, `time`, `typing`), psycopg for DB, pytest with the `migrated_url` Postgres fixture (handler tests) and pure unit tests (provider). Guardrails: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`. `ty` checks src **and** tests (hard-gating); SQL helper params typed `LiteralString`; the libvirt connect seam ignores `invalid-argument-type` (not `unresolved-import`).

**Design source:** [`docs/superpowers/specs/2026-06-04-connect-plane-gdbstub-design.md`](../specs/2026-06-04-connect-plane-gdbstub-design.md) · [`docs/adr/0032-connect-plane-gdbstub-debugsession.md`](../../adr/0032-connect-plane-gdbstub-debugsession.md)

**Reference patterns (read before starting):**
- v1 sources: `~/src/kdive-v1/src/kdive/transport/backends/qemu_gdbstub.py` (loopback F2 control, RSP probe orchestration), `~/src/kdive-v1/src/kdive/transport/core/rsp_probe.py` (`rsp_frame`/`valid_rsp_frame`/`rsp_reachable` — verbatim port), `~/src/kdive-v1/src/kdive/transport/core/bounded.py` (`Deadline`/`connect_tcp` — port only what the probe needs).
- `src/kdive/providers/local_libvirt/control.py` (the realized-port + `from_env` lazy-seam + `Protocol` house style; the `_detach_sessions` join this plane coordinates with).
- `src/kdive/providers/local_libvirt/retrieve.py` (the `live_vm`-gated `# pragma: no cover` seam + `MISSING_DEPENDENCY` default pattern).
- `src/kdive/mcp/tools/control.py` + `src/kdive/mcp/tools/investigations.py` (tool handler idiom: `_config_error`, `_as_uuid`, `bind_context`, `advisory_xact_lock`, `audit.record`, `register`).
- `tests/mcp/test_control_tools.py` (the `_pool`/`_granted_allocation`/`_seed_system`/`_seed_live_session` seeding helpers — reuse the shapes), `tests/providers/local_libvirt/test_control.py` (provider unit-test idiom).

---

## File Structure

- **Create** `src/kdive/providers/local_libvirt/connect.py` — RSP helpers + `Connector`/`LocalLibvirtConnect` + `TransportHandleData` (codec) + `live_vm`-gated real resolver/prober seams.
- **Create** `src/kdive/mcp/tools/debug.py` — `start_session`/`end_session` async handlers + `register(app, pool)`.
- **Modify** `src/kdive/mcp/app.py` — append `debug.register` to `_PLANE_REGISTRARS` (one line; shared file with #22 — additive only).
- **Create** `tests/providers/local_libvirt/test_connect.py` — provider unit tests (fakes).
- **Create** `tests/mcp/test_debug_tools.py` — handler tests over `migrated_url`.
- **Modify** `tests/mcp/test_app.py` — assert `debug.start_session`/`debug.end_session` are registered (two assertions; shared file with #22 — additive only).

No schema migration; no `mcp/responses.py`, `domain/models.py`, `domain/state.py`, `db/repositories.py`, or `0001_init.sql` change (all the durable machinery already exists on `main`).

---

## Task 1: Port the RSP-framing + bounded-probe primitives into `connect.py`

**Files:** Create `src/kdive/providers/local_libvirt/connect.py`; Create `tests/providers/local_libvirt/test_connect.py`

Port the smallest viable subset of v1's `rsp_probe.py` + `bounded.py` — the framing codec and a bounded RSP-reachability probe. These are pure (no DB, no MCP) and fully unit-testable.

- [ ] **Step 1: Write the failing tests** (`tests/providers/local_libvirt/test_connect.py`):
  - `rsp_frame("?")` yields `b"$?#3f"` (mod-256 checksum, 2 hex digits).
  - `valid_rsp_frame` is True for a complete checksum-valid `$...#xx`, False for a bare `+`, a truncated `$...` with no `#`, a non-hex checksum, and a checksum mismatch.
  - `valid_rsp_frame` ignores a leading `+` ack before the frame.
- [ ] **Step 2: Implement** — port `rsp_frame`/`valid_rsp_frame` verbatim (semantics-preserving), plus `Deadline` (the `after`/`remaining`/`expired` monotonic clock) and a `connect_tcp` the probe uses. Drop every v1 helper the probe does not need (`spawn`, `open_device`, `await_accept`, `allocate_loopback_ports`, `wait_for_listener`). Keep `RSP_MAX_ACCUMULATE_BYTES` as the hostile-peer bound.
- [ ] **Step 3: Guardrails green** — `ruff check`, `ruff format`, `ty check src`, `pytest -q tests/providers/local_libvirt/test_connect.py`. The accumulate loop and `recv` paths the unit tests cannot drive without a socket are `# pragma: no cover - live_vm`.

## Task 2: The `Connector` port + `TransportHandleData` codec + loopback/probe orchestration

**Files:** `src/kdive/providers/local_libvirt/connect.py` (extend); `tests/providers/local_libvirt/test_connect.py` (extend)

Build the realized `Connector` on the Task-1 primitives, with injected `live_vm`-gated seams.

- [ ] **Step 1: Write the failing tests** (inject a fake resolver + fake prober — no real socket):
  - `TransportHandleData` round-trips through its serialize/parse codec (`kind`/`host`/`port`); a malformed serialized handle raises a categorized `CONFIGURATION_ERROR` (not a bare `ValueError`).
  - `open_transport(system, "tcp")` (any non-`gdbstub` kind) raises `CONFIGURATION_ERROR` **without** invoking the prober (assert the fake prober was never called).
  - A resolver returning a non-loopback host (`10.0.0.1`) or a hostname (`evil.example`) raises `CONFIGURATION_ERROR` **before** the prober runs (assert prober not called) — the v1 F2 control.
  - A loopback host + a prober that returns False raises `DEBUG_ATTACH_FAILURE`.
  - A loopback host + a prober that raises a socket/OS error raises `TRANSPORT_FAILURE`.
  - A loopback host + a prober that returns True returns a `TransportHandle` whose decoded `host`/`port` match the resolved endpoint.
  - The default `from_env()` resolver raises `MISSING_DEPENDENCY` (no `live_vm` host) — assert the category.
  - `close_transport(handle)` is a no-op that never raises, even on a malformed handle.
- [ ] **Step 2: Implement** `LocalLibvirtConnect`:
  - `Connector` Protocol (`open_transport(system, kind) -> TransportHandle`, `close_transport(handle) -> None`); `TransportHandleData` NamedTuple + a deterministic string codec (e.g. `gdbstub://host:port`, parsed back with categorized errors).
  - Constructor takes injected `resolve_endpoint: Callable[[SystemHandle], tuple[str, int]]` and `probe: Callable[[str, int], bool]` seams; `from_env()` defaults both to `# pragma: no cover - live_vm` reals (resolver raises `MISSING_DEPENDENCY`; prober wraps the Task-1 `rsp_reachable` against a real socket).
  - `open_transport`: reject non-`gdbstub` kind → `CONFIGURATION_ERROR`; resolve endpoint; loopback-only check via `ipaddress.ip_address(host).is_loopback` (hostname → not-an-IP → reject), raising `CONFIGURATION_ERROR` **before** probing; probe → False = `DEBUG_ATTACH_FAILURE`, `OSError` = `TRANSPORT_FAILURE`, True = return the encoded handle. A resolver `CategorizedError(MISSING_DEPENDENCY)` propagates (the tool maps it to `debug_attach_failure`).
  - `close_transport`: best-effort no-op (connectionless RSP in M0); swallow any error.
- [ ] **Step 3: Guardrails green** — full provider test file passes; `ty check src` clean (libvirt seam, if any, ignores `invalid-argument-type`, not `unresolved-import`).

## Task 3: `debug.start_session` handler

**Files:** Create `src/kdive/mcp/tools/debug.py`; Create `tests/mcp/test_debug_tools.py`

Add the start-session handler. Reuse the `test_control_tools.py` seeding helpers (copy the `_pool`/`_granted_allocation`/`_seed_system`/`_ctx` shapes into the new test module — do **not** import across test modules unless a shared `_seed` helper already covers it).

- [ ] **Step 1: Write the failing tests** (`tests/mcp/test_debug_tools.py`), each over `migrated_url`, calling `debug.start_session(pool, ctx, run_id=..., transport=...)` directly with a `_FakeConnector` injected:
  - Happy path: a `succeeded` Run with a succeeded `boot` step on a `ready` System + a fake connector that opens a handle → exactly one `debug_sessions` row in `live` with non-null `transport_handle` + `worker_heartbeat_at`; envelope `status == "live"`; `->attach` and `attach->live` audit rows present.
  - Second attach (any Run on the same System with an existing `live` session) → `status == "error"`, `error_category == "transport_conflict"`, **no** new row (assert row count unchanged), and the fake connector's `close_transport` was called (lost-race cleanup).
  - Run not `succeeded` (e.g. `created`) → `configuration_error` with `current_status`; no row; prober/connector **not** invoked.
  - Run `succeeded` but **no** succeeded `boot` step → `configuration_error` `reason="boot_first"`; no row.
  - System not `ready` (e.g. `defined`) → `configuration_error` `current_status`; no row.
  - `transport != "gdbstub"` → `configuration_error`; no row; connector not invoked.
  - Connector raises `DEBUG_ATTACH_FAILURE` → envelope `debug_attach_failure`; no row.
  - Connector raises `TRANSPORT_FAILURE` → `transport_failure`; no row.
  - Connector raises `MISSING_DEPENDENCY` → mapped to `debug_attach_failure`; no row.
  - Cross-project / malformed-UUID `run_id` → `configuration_error`; no row.
  - Missing `operator` (viewer ctx) → raises `AuthorizationError`.
- [ ] **Step 2: Implement** `start_session(pool, ctx, *, run_id, transport="gdbstub")`:
  - `_as_uuid` + `_config_error` helpers (mirror `control.py`). Resolve the Run; `None`/cross-project → `_config_error`. `require_role(ctx, run.project, Role.OPERATOR)`.
  - Reject non-`gdbstub` transport. Run-boot guard: `run.state is RunState.SUCCEEDED` else `_config_error(current_status)`; a succeeded `boot` `run_steps` row exists else `_config_error(reason="boot_first")` (reuse the `_has_succeeded_step` pattern from `runs.py` — a `LiteralString` query).
  - Pre-lock read of the Run's System (`SYSTEMS.get`): not `ready` → `_config_error(current_status)`; an existing `attach`/`live` session on the System → `transport_conflict` (the join query, `LiteralString`).
  - Open the transport **outside** any transaction: `connector.open_transport(system_handle, "gdbstub")` wrapped to map `CategorizedError` by category (`DEBUG_ATTACH_FAILURE`/`TRANSPORT_FAILURE`/`MISSING_DEPENDENCY → debug_attach_failure`).
  - Under `conn.transaction()` + `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`: re-check the single-attach join and System-ready authoritatively; on failure `connector.close_transport(handle)` then return the categorized error. Else insert the `DebugSession` (`state=attach`, `transport="gdbstub"`, `transport_handle=handle`, `worker_heartbeat_at=now`), `DEBUG_SESSIONS.update_state(... LIVE)`, audit `->attach` and `attach->live`. Return `ToolResponse.success(session_id, "live", suggested_next_actions=["debug.end_session"], ...)`.
- [ ] **Step 3: Guardrails green** — `pytest -q tests/mcp/test_debug_tools.py`, `ruff`, `ty check src`.

## Task 4: `debug.end_session` handler + `register`

**Files:** `src/kdive/mcp/tools/debug.py` (extend); `tests/mcp/test_debug_tools.py` (extend)

- [ ] **Step 1: Write the failing tests:**
  - `end_session` on a `live` session → `status == "detached"`; row `detached`; one `live->detached` audit row; the fake connector's `close_transport` was called.
  - `end_session` again (already `detached`) → idempotent `status == "detached"`; **no** second audit row.
  - `end_session` on an `attach` session → `detached`; `attach->detached` audit.
  - Cross-project / malformed-UUID `session_id` → `configuration_error`.
  - Session whose Run/System vanished → `configuration_error`.
  - Missing `operator` → raises `AuthorizationError`.
  - `close_transport` raising does not break the detach (row still reaches `detached`).
- [ ] **Step 2: Implement** `end_session(pool, ctx, session_id)`:
  - Resolve the session (`DEBUG_SESSIONS.get`); `None`/cross-project → `_config_error`. `require_role(OPERATOR)`.
  - Resolve the System id via a `debug_sessions → runs` join (`LiteralString`); a vanished Run/System → `_config_error`.
  - Under `conn.transaction()` + `advisory_xact_lock(SYSTEM, system_id)`: read the session `FOR UPDATE`; `detached` → idempotent success; else `connector.close_transport(handle)` (best-effort, swallow), `DEBUG_SESSIONS.update_state(... DETACHED)`, audit `{old}->detached`, success.
  - Decode the stored `transport_handle` to pass to `close_transport`; a missing/unparseable handle still detaches (close is best-effort).
- [ ] **Step 3: Implement `register(app, pool)`** mirroring `control.register`: two `@app.tool` wrappers (`debug.start_session`, `debug.end_session`) calling the handlers with `current_context()`. Build the `Connector` lazily via `LocalLibvirtConnect.from_env()` (no libvirt connection at registration), injected into the handlers (a module-level default the tools close over, overridable in tests).
- [ ] **Step 4: Guardrails green** — full `tests/mcp/test_debug_tools.py` passes; `ruff`, `ty check src`.

## Task 5: Register the plane in `app.py` + assert in `test_app.py`

**Files:** Modify `src/kdive/mcp/app.py`; Modify `tests/mcp/test_app.py`

Shared files with #22 — keep edits minimal and additive.

- [ ] **Step 1: Write the failing test** — extend `tests/mcp/test_app.py`: assert `await app.list_tools()` includes `debug.start_session` and `debug.end_session` (mirror the existing per-plane registration assertions; dotted names + `await app.list_tools()` is the working pattern). No handler-registry assertion (synchronous tools register no `JobKind`).
- [ ] **Step 2: Implement** — import `debug` in `app.py` and append `debug.register` to `_PLANE_REGISTRARS` only (do **not** touch `_HANDLER_REGISTRARS`). Place the import alphabetically near `control`; do not reorder the existing tuple entries (minimize the diff #22 also edits).
- [ ] **Step 3: Guardrails green** — `pytest -q tests/mcp/test_app.py`, `ruff`, `ty check src`.

## Task 6: Full-suite guardrails + zero-warnings sweep

**Files:** none (verification)

- [ ] **Step 1:** `uv run ruff check` and `uv run ruff format --check` — clean.
- [ ] **Step 2:** `uv run ty check src` — clean (pre-commit additionally checks tests; the external worktree keeps the ty hook clean, no `--no-verify`). Any libvirt seam suppression is `invalid-argument-type` at the connect line, never `unresolved-import`.
- [ ] **Step 3:** `uv run python -m pytest -q` — full suite green with **no** `live_vm` host (every real socket/libvirt path is a gated `# pragma: no cover - live_vm` seam). Confirm no `live_vm`-marked test was un-gated.
- [ ] **Step 4:** After each commit, verify `git log -1` is the intended HEAD (prek can silently roll back a commit when `ruff format` rewrites a file with unstaged changes).

---

## Rollback / cleanup

Every task is additive (two new modules, two new test files, one one-line `app.py` append, two `test_app.py` assertions) — no migration, no edits to durable models/state/repositories. Rollback is `git revert` of the feature commits; nothing external is mutated (no DB schema change, no object-store writes, no libvirt calls in the unit-tested path). A mid-way abort leaves `main` unaffected (work is on the feature branch/worktree).

## Verification gaps consciously accepted

- The **real** libvirt-domain gdbstub endpoint resolver and the **real** socket RSP probe run only under `live_vm` (acceptance is `live_vm`-gated by the issue). CI exercises the full orchestration + error contract with fakes; the host path is covered by the `live_vm` acceptance, not unit tests.
- `worker_heartbeat_at` liveness has no consumer in M0 (the dead-session sweep is M1.5); the tests assert only that it is set non-null at attach, not any aging behavior.
