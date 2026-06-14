# Dead-worker gdbstub reconciler reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the reconciler detaches a stale `live` DebugSession whose worker died, also reset the remote gdbstub transport so the freed port no longer blocks the next `debug.start_session` attach with `transport_conflict`.

**Architecture:** A new narrow `TransportResetter` provider port (mirroring `InfraReaper`) injected into the reconciler. `reconciler/loop.py` (the one gate-protected core file, allowlist-extended in this PR) detaches as before, applies a live-holder guard, then calls the injected resetter best-effort. `RemoteLibvirtTransportResetter` self-selects remote gdbstub sessions and re-arms the stub over `qemu+tls` (stop-then-rearm); `NullResetter` is the default. Composition + `__main__` wire it like the reaper.

**Tech Stack:** Python 3.13, `uv`, `psycopg` (async), `libvirt`/`libvirt_qemu`, pytest (+ disposable Postgres via testcontainers). Guardrails: `just lint`, `just type`, `just test`, `just m2-gate`.

**Spec:** [`../specs/2026-06-09-dead-worker-gdbstub-reset-design.md`](../specs/2026-06-09-dead-worker-gdbstub-reset-design.md) · **ADR:** [ADR-0086](../../adr/0086-dead-worker-gdbstub-reconciler-reset.md) · **Issue:** #216

**Conventions every task must honor (CLAUDE.md / AGENTS.md):**
- Run on the feature branch `feat/dead-worker-gdbstub-reset-216`. Never commit to `main`.
- Absolute imports only (`from kdive...`), Google-style docstrings on public APIs, ≤100-char lines, ruff lint set `E,F,I,UP,B,SIM`, `ty` strict.
- Pick the most specific existing `ErrorCategory`; never invent strings. Plain factual prose in docs/comments (no "robust/comprehensive/critical/elegant").
- TDD: failing test first, confirm the expected failure, minimal implementation, re-run. Run `just lint && just type && <focused tests>` before every commit. Commit messages: Conventional Commits, imperative ≤72-char subject, end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## File structure

- **Create** `src/kdive/providers/transport_reset.py` — the `TransportResetter` Protocol + `NullResetter` (NOT a gated core prefix).
- **Create** `src/kdive/providers/remote_libvirt/transport_reset.py` — `RemoteLibvirtTransportResetter` (not gated).
- **Modify** `src/kdive/reconciler/loop.py` — thread `resetter`; widen `_repair_dead_sessions` (the ONLY gated core file touched).
- **Modify** `scripts/m2_portability_gate.py` — add `src/kdive/reconciler/loop.py` to `ALLOWED_FILES`.
- **Modify** `src/kdive/providers/composition.py` — `build_reconciler_transport_resetter()` (not gated).
- **Modify** `src/kdive/__main__.py` — pass the resetter to `Reconciler` (not gated).
- **Test** `tests/providers/test_transport_reset.py`, `tests/providers/remote_libvirt/test_transport_reset.py`, `tests/reconciler/test_loop.py` (+ `tests/reconciler/conftest.py` helper), `tests/providers/test_composition.py` (or the existing composition test module).

---

### Task 1: The `TransportResetter` port + `NullResetter`

**Files:**
- Create: `src/kdive/providers/transport_reset.py`
- Test: `tests/providers/test_transport_reset.py`

- [ ] **Step 1: Write the failing test**

```python
"""TransportResetter port + NullResetter default (#216, ADR-0086)."""

from __future__ import annotations

import asyncio

from kdive.providers.transport_reset import NullResetter, TransportResetter


def test_null_resetter_satisfies_the_port() -> None:
    assert isinstance(NullResetter(), TransportResetter)


def test_null_resetter_reset_is_a_noop() -> None:
    async def scenario() -> None:
        # No transport touched; returns None for every input shape.
        assert (
            await NullResetter().reset(
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
                domain_name="kdive-sys",
            )
            is None
        )
        assert (
            await NullResetter().reset(
                transport="drgn-live", transport_handle=None, domain_name=None
            )
            is None
        )

    asyncio.run(scenario())
```

**Repo async-test convention (verified):** this repo has no `asyncio_mode`/`anyio` pytest config
and uses **no** `async def test` functions. Every test is a synchronous `def test_...` that
wraps its async body in an inner `async def scenario()` and calls `asyncio.run(scenario())` (see
`tests/reconciler/test_loop.py` and `tests/providers/remote_libvirt/test_retrieve.py`). Use this
idiom for **all** async assertions in this plan — never `@pytest.mark.anyio`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/test_transport_reset.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.providers.transport_reset'`.

- [ ] **Step 3: Write minimal implementation**

```python
"""The reconciler's provider port for resetting a dead session's transport (#216, ADR-0086).

When the reconciler detaches a stale ``live`` DebugSession whose worker died, a remote
provider's single-client gdbstub can still be held by the dead worker's lingering TCP
connection (ADR-0079). This narrow port lets the reconciler reset that transport without
importing a provider — mirroring :mod:`kdive.providers.reaping`. ``NullResetter`` is the
default (local-libvirt's co-located gdbstub is freed by the host OS on worker death).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TransportResetter(Protocol):
    """Reset a detached dead session's transport so its port stops blocking re-attach.

    The reconciler passes only core-available data; the concrete resetter self-selects the
    sessions it owns (e.g. remote gdbstub) and no-ops the rest.
    """

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None: ...


class NullResetter:
    """The default resetter: touches no transport (local-libvirt needs no active reset)."""

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/test_transport_reset.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type && uv run python -m pytest tests/providers/test_transport_reset.py -q`
Expected: all green.

```bash
git add src/kdive/providers/transport_reset.py tests/providers/test_transport_reset.py
git commit -m "feat: add TransportResetter reconciler port + NullResetter default (#216)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Thread the resetter through the reconciler + extend the gate allowlist

This is the gate-protected core change. It widens `_repair_dead_sessions` to (a) return the
detached rows' transport/handle/run_id, (b) apply the live-holder guard, (c) resolve the
System's `domain_name`, (d) call the resetter best-effort — and threads a `resetter` parameter
through `reconcile_once` and `Reconciler`. It also adds `reconciler/loop.py` to the M2 gate
allowlist (the file is now a deliberate, ADR-0086-reviewed core touch).

**Files:**
- Modify: `src/kdive/reconciler/loop.py` (`_repair_dead_sessions`, `_repair_plan`, `reconcile_once`, `Reconciler`)
- Modify: `scripts/m2_portability_gate.py` (`ALLOWED_FILES`)
- Modify: `tests/reconciler/conftest.py` (extend `seed_debug_session` with `transport_handle` + add a `FakeResetter`)
- Test: `tests/reconciler/test_loop.py`

- [ ] **Step 1: Extend the test conftest with a `FakeResetter` and a `transport_handle` seed param**

In `tests/reconciler/conftest.py`, add a recording resetter near `FakeReaper`:

```python
class FakeResetter:
    """Records reset(...) calls; structurally satisfies TransportResetter (duck-typed).

    ``fail`` makes ``reset`` raise after recording, to prove a reset failure does not
    starve the rest of the dead-session sweep.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, str | None]] = []
        self._fail = fail

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        self.calls.append(
            {"transport": transport, "transport_handle": transport_handle, "domain_name": domain_name}
        )
        if self._fail:
            raise RuntimeError("boom")
```

In the same file, extend `seed_debug_session` to accept an optional handle and set it in SQL:

```python
async def seed_debug_session(
    conn: psycopg.AsyncConnection,
    run_id: UUID,
    *,
    state: DebugSessionState = DebugSessionState.LIVE,
    heartbeat_ago: timedelta | None = None,
    transport: str = "gdbstub",
    transport_handle: str | None = None,
) -> UUID:
```

Pass `transport=transport` into the `DebugSession(...)` constructor (replacing the hard-coded
`transport="gdbstub"`), and after the existing `heartbeat_ago` UPDATE, add:

```python
    if transport_handle is not None:
        await conn.execute(
            "UPDATE debug_sessions SET transport_handle = %s WHERE id = %s",
            (transport_handle, session.id),
        )
    return session.id
```

(Confirm the `DebugSession` dataclass accepts `transport` as a field; it already stores
`transport="gdbstub"` today, so only the value becomes parameterized.)

- [ ] **Step 2: Write the failing tests**

In `tests/reconciler/test_loop.py`, update the `_detach` helper to take a resetter, and add new
tests. First the helper (replace the existing `_detach` at ~line 258):

```python
def _detach(stale_after: timedelta, resetter=None):
    from kdive.providers.transport_reset import NullResetter

    r = resetter if resetter is not None else NullResetter()
    return lambda conn: loop._repair_dead_sessions(conn, stale_after, r)
```

The existing dead-session tests already call `_detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER)`;
the optional `resetter=None` default keeps them passing (NullResetter) with no edit. Add
`FakeResetter` to the conftest import line at the top of `test_loop.py` (the same line that
imports `seed_system`, `seed_run`, `seed_debug_session`). Then add these tests, which reuse the
exact `connect` / `run_repair` / `AsyncConnectionPool` idiom of `test_stale_live_session_detached`
above — `run_repair(pool, _detach(...))` runs the repair on a pooled (non-autocommit) connection:

```python
def test_stale_gdbstub_session_triggers_a_reset(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        resetter = FakeResetter()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, resetter)
            )
        assert count == 1
        assert resetter.calls == [
            {
                "transport": "gdbstub",
                "transport_handle": "gdbstub://10.0.0.5:1234",
                "domain_name": "kdive-sys",
            }
        ]

    asyncio.run(_run())


def test_live_holder_guard_skips_reset(migrated_url: str) -> None:
    """A System with a fresh live gdbstub session is not reset (no eviction)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            stale_run = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                stale_run,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
            fresh_run = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                fresh_run,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(seconds=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        resetter = FakeResetter()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, resetter)
            )
        assert count == 1  # the one stale session is still detached
        assert resetter.calls == []  # but the fresh live gdbstub holder => reset skipped

    asyncio.run(_run())


def test_reset_failure_does_not_strand_the_detach(migrated_url: str) -> None:
    """A raising resetter is swallowed; the session is still detached and the count stands."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, FakeResetter(fail=True))
            )
        assert count == 1  # the reset raised but the detach stands
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "detached"

    asyncio.run(_run())
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: FAIL — `_repair_dead_sessions()` takes 2 positional args but 3 were given (the
signature has not been widened yet).

- [ ] **Step 4: Implement the reconciler change**

In `src/kdive/reconciler/loop.py`:

1. Add the import: `from kdive.providers.transport_reset import NullResetter, TransportResetter`.

2. Replace `_repair_dead_sessions` with:

```python
async def _repair_dead_sessions(
    conn: AsyncConnection, stale_after: timedelta, resetter: TransportResetter
) -> int:
    """Detach stale ``live`` debug sessions, then reset each dead transport best-effort.

    A NULL heartbeat is never swept (a just-attached session that has not beaten yet).
    ``stale_after`` is the provisional cadence contract (ADR-0021). After the detach commits,
    each detached session's transport is reset (ADR-0086) so a dead worker's single-client
    gdbstub does not block the next attach with ``transport_conflict`` — best-effort: a reset
    failure is logged and the sweep continues, and a System that already has a fresh ``live``
    gdbstub holder is skipped so a legitimate re-attach is never evicted.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = %s "
            "WHERE state = %s AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s "
            "RETURNING id, run_id, transport, transport_handle",
            (_DETACHED_DEBUG_SESSION_STATE_VALUE, _LIVE_DEBUG_SESSION_STATE_VALUE, stale_after),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
        await _reset_dead_transport(conn, resetter, row)
    return len(rows)


async def _reset_dead_transport(
    conn: AsyncConnection, resetter: TransportResetter, row: dict
) -> None:
    """Reset one detached session's transport, guarded and best-effort (ADR-0086)."""
    system_id, domain_name = await _resolve_system(conn, row["run_id"])
    if system_id is not None and await _has_live_gdbstub_holder(conn, system_id):
        _log.info(
            "reconciler: session %s detached but System %s has a live gdbstub holder; "
            "skipping transport reset",
            row["id"],
            system_id,
        )
        return
    try:
        await resetter.reset(
            transport=row["transport"],
            transport_handle=row["transport_handle"],
            domain_name=domain_name,
        )
    except Exception:  # noqa: BLE001 - a reset failure must not starve the rest of the sweep
        _log.warning(
            "reconciler: resetting dead transport for session %s failed; the next attach "
            "may contend (transport_conflict)",
            row["id"],
            exc_info=True,
        )


async def _resolve_system(conn: AsyncConnection, run_id: UUID) -> tuple[UUID | None, str | None]:
    """Return ``(system_id, domain_name)`` for a Run, or ``(None, None)`` if the Run is gone."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id AS system_id, s.domain_name "
            "FROM runs r JOIN systems s ON s.id = r.system_id WHERE r.id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    return row["system_id"], row["domain_name"]


async def _has_live_gdbstub_holder(conn: AsyncConnection, system_id: UUID) -> bool:
    """True if any debug session for ``system_id`` is currently ``live`` on the gdbstub transport.

    A live holder means the single-client port is legitimately occupied (a new debugger won the
    freed port after our detach), so re-arming it would evict that client (ADR-0086).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM debug_sessions ds JOIN runs r ON r.id = ds.run_id "
            "WHERE r.system_id = %s AND ds.state = %s AND ds.transport = %s LIMIT 1",
            (system_id, _LIVE_DEBUG_SESSION_STATE_VALUE, "gdbstub"),
        )
        return await cur.fetchone() is not None
```

3. Thread the resetter through `_repair_plan` and `reconcile_once`:

In `_repair_plan(...)` add a `resetter: TransportResetter` keyword parameter and change the
dead-session spec lambda to:

```python
        _RepairSpec(
            "dead_sessions",
            lambda conn: _repair_dead_sessions(conn, debug_session_stale_after, resetter),
        ),
```

In `reconcile_once(...)` add `resetter: TransportResetter = NullResetter()` to the signature and
pass `resetter=resetter` into the `_repair_plan(...)` call.

In `Reconciler.__init__` add `resetter: TransportResetter = NullResetter()`, store
`self._resetter = resetter`, and pass `resetter=self._resetter` in `run_once`'s
`reconcile_once(...)` call.

- [ ] **Step 5: Extend the M2 gate allowlist**

In `scripts/m2_portability_gate.py`, add to `ALLOWED_FILES` (after the `introspect.py` entry):

```python
        # Dead-worker gdbstub reconciler reset (#216, ADR-0086): the deliberate, reviewed core
        # touch that resets a stale session's transport through the injected TransportResetter
        # port so a dead worker's single-client gdbstub stops blocking re-attach.
        "src/kdive/reconciler/loop.py",
```

- [ ] **Step 6: Run tests + the gate to verify green**

Run: `uv run python -m pytest tests/reconciler/test_loop.py -q`
Expected: PASS (new + existing dead-session tests).

Run: `git add -A && just m2-gate` (the gate needs the `pre-M2` tag fetched: `git fetch --tags origin` if it reports exit 2).
Expected: gate passes — `reconciler/loop.py` is now allowlisted; no other non-allowlisted core file touched.

- [ ] **Step 7: Lint, type, full focused suite, commit**

Run: `just lint && just type && uv run python -m pytest tests/reconciler -q`
Expected: all green.

```bash
git add src/kdive/reconciler/loop.py scripts/m2_portability_gate.py tests/reconciler/
git commit -m "feat: reset the dead-worker gdbstub transport in the reconciler (#216)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `RemoteLibvirtTransportResetter`

The concrete remote resetter: self-select gdbstub sessions whose handle host equals the
operator `gdb_addr`, then re-arm the stub over `qemu+tls` with the stop-then-rearm sequence. The
slow monitor call is an injected `live_vm`-gated seam; unit tests drive orchestration with fakes.

**Files:**
- Create: `src/kdive/providers/remote_libvirt/transport_reset.py`
- Modify: `tests/providers/remote_libvirt/fakes.py` (add `qemuMonitorCommand` to `FakeDomain`)
- Test: `tests/providers/remote_libvirt/test_transport_reset.py`

- [ ] **Step 1: Add a monitor seam to the test `FakeDomain`**

In `tests/providers/remote_libvirt/fakes.py`, add to `FakeDomain` (record monitor commands):

```python
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str:  # noqa: N802 - libvirt binding name
        self.calls.append(f"monitor:{cmd}")
        self._maybe_raise("qemuMonitorCommand")
        return ""
```

- [ ] **Step 2: Write the failing tests**

Create `tests/providers/remote_libvirt/test_transport_reset.py`:

```python
"""RemoteLibvirtTransportResetter tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

import asyncio
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_GDB_ADDR = "10.0.0.5"
_DOMAIN = "kdive-sys"


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        gdb_addr=_GDB_ADDR,
    )


def _resetter(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtTransportResetter:
    conn = FakeControlConn({_DOMAIN: domain} if domain is not None else {})
    return RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


def test_matching_gdbstub_handle_rearms_with_stop_then_start(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == ["monitor:gdbserver none", "monitor:gdbserver tcp::1234"]


def test_non_gdbstub_transport_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="drgn-live", transport_handle=_DOMAIN, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_handle_host_not_gdb_addr_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle="gdbstub://127.0.0.1:1234",  # a local loopback session, not ours
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_missing_domain_name_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=f"gdbstub://{_GDB_ADDR}:1234", domain_name=None
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_none_handle_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=None, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_monitor_error_maps_to_transport_failure(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN, raise_on={"qemuMonitorCommand": libvirt.VIR_ERR_OPERATION_FAILED})

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    with pytest.raises(CategorizedError) as exc:
        asyncio.run(scenario())
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
```

Check `RemoteLibvirtConfig`'s field names (`concurrent_allocation_cap`, `gdb_addr`) against
`config.py` and the existing `test_control.py::_config`; match them exactly.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_transport_reset.py -q`
Expected: FAIL — `ModuleNotFoundError: ...transport_reset`.

- [ ] **Step 4: Implement `RemoteLibvirtTransportResetter`**

Create `src/kdive/providers/remote_libvirt/transport_reset.py`:

```python
"""Remote-libvirt transport reset: re-arm a dead worker's gdbstub (#216, ADR-0086).

When the reconciler detaches a stale ``live`` DebugSession, this resetter frees the System's
single-client gdbstub so the next attach is not blocked by the dead worker's lingering
connection (ADR-0079). It self-selects: only a ``gdbstub`` transport whose handle host equals
the operator ``gdb_addr`` and that carries a domain name is re-armed; everything else is a
no-op. The re-arm is the explicit stop-then-rearm (``gdbserver none`` then
``gdbserver tcp::<port>``) over the ``qemu+tls`` monitor, closing the holding connection
deterministically (ADR-0083 §2 host policy; ADR-0077 connection lifecycle). The monitor call
runs only under the ``live_vm`` gate; orchestration + self-selection are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)
_GDBSTUB = "gdbstub"


class _Domain(Protocol):
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str: ...  # noqa: N802 - libvirt name


class _ResetConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenResetConnection = Callable[[str], _ResetConn]


def open_libvirt_reset(uri: str) -> _ResetConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


def _real_rearm(domain: _Domain, port: int) -> None:  # pragma: no cover - live_vm
    """Stop-then-rearm the gdbstub over the QEMU monitor (HMP), dropping the stale client."""
    import libvirt_qemu  # ty: ignore[unresolved-import] - C-extension, no stubs (per CLAUDE.md)

    hmp = libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_HMP
    domain.qemuMonitorCommand("gdbserver none", hmp)
    domain.qemuMonitorCommand(f"gdbserver tcp::{port}", hmp)


class RemoteLibvirtTransportResetter:
    """Re-arm a dead worker's remote gdbstub so the freed port no longer blocks re-attach."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenResetConnection = open_libvirt_reset,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        rearm: Callable[[_Domain, int], None] = _real_rearm,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._rearm = rearm
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtTransportResetter:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        """Re-arm the gdbstub if this is a matching remote gdbstub session; else no-op.

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` if the monitor re-arm errors.
        """
        port = self._port_if_ours(transport, transport_handle, domain_name)
        if port is None:
            return
        assert domain_name is not None  # narrowed by _port_if_ours
        await asyncio.to_thread(self._rearm_blocking, domain_name, port)
        _log.info("reconciler: re-armed remote gdbstub for domain %s (port %d)", domain_name, port)

    def _port_if_ours(
        self, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> int | None:
        if transport != _GDBSTUB:
            return None
        if transport_handle is None:
            _log.info("reconciler: gdbstub session has no handle; skipping reset")
            return None
        try:
            data = TransportHandleData.decode(transport_handle)
        except CategorizedError:
            _log.info("reconciler: undecodable transport handle; skipping reset")
            return None
        config = self._config_factory()
        if data.kind != _GDBSTUB or data.host != config.gdb_addr:
            return None  # a local loopback gdbstub, or not our gdb_addr — not ours to reset
        if domain_name is None:
            _log.info("reconciler: remote gdbstub session has no domain_name; cannot reset")
            return None
        return data.port

    def _rearm_blocking(self, domain_name: str, port: int) -> None:
        with self._connection() as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    f"looking up domain {domain_name!r} for gdbstub reset failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                ) from exc
            try:
                self._rearm(domain, port)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "re-arming the remote gdbstub failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                    details={"port": port},
                ) from exc

    def _connection(self) -> AbstractContextManager[_ResetConn]:
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )


__all__ = ["RemoteLibvirtTransportResetter"]
```

Verify `TransportHandleData` is exported from `kdive.providers.ports` (it is — see
`local_libvirt/lifecycle/connect.py`). Verify `remote_connection`'s generic opener type accepts
the `_ResetConn` slice (it is generic over `ClosableConn`; `_ResetConn` has `close`, so it
satisfies the bound).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_transport_reset.py -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Lint, type, commit**

Run: `just lint && just type && uv run python -m pytest tests/providers/remote_libvirt -q`
Expected: all green. `ty` is whole-tree. `import libvirt` resolves bare (as in `control.py`);
`open_libvirt_reset` already carries `# ty: ignore[invalid-return-type]` on the `libvirt.open`
return (matching `control.py`). The function-local `import libvirt_qemu` in `_real_rearm` is the
one unresolvable import and already carries `# ty: ignore[unresolved-import]` in the code block
above. If `ty` surfaces any other libvirt-family diagnostic, mirror the scoped per-site ignore
used in `control.py`/`connect.py`.

```bash
git add src/kdive/providers/remote_libvirt/transport_reset.py tests/providers/remote_libvirt/
git commit -m "feat: add RemoteLibvirtTransportResetter (gdbstub re-arm) (#216)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire the resetter through composition + `__main__`

**Files:**
- Modify: `src/kdive/providers/composition.py` (`build_reconciler_transport_resetter`)
- Modify: `src/kdive/__main__.py` (pass it to `Reconciler`)
- Test: the existing composition test module (find it with `rg -l build_reconciler_reaper tests/`)

- [ ] **Step 1: Write the failing test**

In the composition test module (the same one that tests `build_reconciler_reaper`), add:

```python
def test_transport_resetter_is_null_without_remote() -> None:
    from kdive.providers.transport_reset import NullResetter

    comp = ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=False)
    assert isinstance(resetter, NullResetter)


def test_transport_resetter_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter

    comp = ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=True)
    assert isinstance(resetter, RemoteLibvirtTransportResetter)
```

Match the existing imports/fixtures in that test module (e.g. how `ProviderComposition` is
constructed for the reaper tests).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest <that test file> -q`
Expected: FAIL — `AttributeError: ... has no attribute 'build_reconciler_transport_resetter'`.

- [ ] **Step 3: Implement the composition method**

In `src/kdive/providers/composition.py`, add the import near the other transport_reset-free
imports:

```python
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.providers.transport_reset import NullResetter, TransportResetter
```

Add a method to `ProviderComposition` (next to `build_reconciler_reaper`):

```python
    def build_reconciler_transport_resetter(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> TransportResetter:
        """Assemble the reconciler's dead-session transport resetter (ADR-0086).

        Returns the remote-libvirt resetter when the remote provider is enabled (operator
        config present or the explicit flag), else the no-op ``NullResetter`` — local-libvirt's
        co-located gdbstub needs no active reset.
        """
        if _remote_libvirt_enabled(enable_remote_libvirt):
            return RemoteLibvirtTransportResetter.from_env(secret_registry=self._secret_registry)
        return NullResetter()
```

- [ ] **Step 4: Wire it into `__main__.py`**

In `src/kdive/__main__.py`, in `_run_reconciler`, pass the resetter to `Reconciler`:

```python
        reconciler = Reconciler(
            pool,
            provider_composition.build_reconciler_reaper(),
            upload_store=upload_store,
            resetter=provider_composition.build_reconciler_transport_resetter(),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest <that test file> tests/reconciler/test_main.py -q`
Expected: PASS.

- [ ] **Step 6: Full guardrail sweep + gate, commit**

Run: `just lint && just type && uv run python -m pytest tests/providers tests/reconciler -q && git add -A && just m2-gate`
Expected: all green; gate passes (composition.py and __main__.py are outside the core prefixes,
so they need no allowlist entry — only `reconciler/loop.py`, added in Task 2).

```bash
git add src/kdive/providers/composition.py src/kdive/__main__.py <that test file>
git commit -m "feat: wire the transport resetter through composition + main (#216)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review checklist (run after implementing all tasks)

- [ ] **Spec coverage** — port (Task 1), reconciler detach+guard+reset+gate (Task 2), remote re-arm + self-selection + error mapping (Task 3), composition/main wiring (Task 4). Topology scoping and the `live_vm` boundary are documented in the spec/ADR (no code artifact). Acceptance #1 (port freed) is unit-covered at the two boundaries + asserted live-only; acceptance #2 (dead-worker → reset → re-attach test, live-holder guard, negatives) is Task 2 + Task 3 tests.
- [ ] **Full CI gate** — `just ci` green locally (lint, type, lint-shell, lint-workflows, check-mermaid, test) and `just m2-gate` green.
- [ ] **No new error strings** — only `TRANSPORT_FAILURE` (existing) is raised; `transport_conflict` is the existing fallback.
- [ ] **Type consistency** — `reset(self, *, transport, transport_handle, domain_name)` identical across `TransportResetter`, `NullResetter`, `RemoteLibvirtTransportResetter`, `FakeResetter`; `build_reconciler_transport_resetter` returns `TransportResetter`.
- [ ] **Docs regen** — this change adds no MCP tool, so `just docs-check` should be unaffected; run it to confirm the generated tool reference is unchanged.
