# Control plane (power + force_crash) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the M0 control plane — `control.power(on|off|cycle|reset)` and gated `control.force_crash` — for the local-libvirt provider, driving System `ready→crashed` and DebugSession `live→detached`.

**Architecture:** A DB-free `LocalLibvirtControl` provider (mirrors `LocalLibvirtProvisioning`, keyed on the libvirt domain name over an injected `Connect` factory) plus a `control.py` MCP plane (mirrors `systems.py`): synchronous tools admit + enqueue jobs; job handlers orchestrate the state machine under the per-System advisory lock. `force_crash` passes the three-check destructive-op gate; `power` is `operator`-authorized and ungated.

**Tech Stack:** Python 3.13, FastMCP, psycopg (async), Pydantic, libvirt-python; pytest; `uv`/`ruff`/`ty`.

**Design source:** [`docs/superpowers/specs/2026-06-04-control-plane-design.md`](../specs/2026-06-04-control-plane-design.md) · **Decisions:** [ADR-0028](../../adr/0028-control-plane-power-force-crash.md)

---

## File structure

- Create `src/kdive/providers/local_libvirt/control.py` — `PowerAction` StrEnum, `Controller` Protocol, `LocalLibvirtControl`.
- Create `src/kdive/mcp/tools/control.py` — `control.power`/`control.force_crash` tools + handlers + `register`/`register_handlers`.
- Modify `src/kdive/profiles/provisioning.py` — add `LibvirtProfile.destructive_ops`.
- Modify `src/kdive/domain/errors.py` — add `ErrorCategory.AUTHORIZATION_DENIED`.
- Modify `src/kdive/mcp/app.py` — append the two control registrars.
- Create `tests/providers/local_libvirt/test_control.py`, `tests/mcp/test_control_tools.py`.
- Modify `tests/providers/local_libvirt/conftest.py` — extend `FakeDomain` with control methods.

Guardrails after every commit: `uv run ruff check && uv run ruff format && uv run ty check src && uv run python -m pytest -q`. After each commit verify HEAD landed (`git log -1 --oneline`).

---

### Task 1: Add `AUTHORIZATION_DENIED` error category

**Files:**
- Modify: `src/kdive/domain/errors.py`
- Test: `tests/domain/test_errors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/domain/test_errors.py`:

```python
def test_authorization_denied_category_value() -> None:
    from kdive.domain.errors import ErrorCategory

    assert ErrorCategory.AUTHORIZATION_DENIED.value == "authorization_denied"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/domain/test_errors.py::test_authorization_denied_category_value -q`
Expected: FAIL with `AttributeError: AUTHORIZATION_DENIED`.

- [ ] **Step 3: Add the category**

In `src/kdive/domain/errors.py`, in the `# New distributed categories` block, after `CONTROL_FAILURE = "control_failure"`:

```python
    AUTHORIZATION_DENIED = "authorization_denied"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/domain/test_errors.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/errors.py tests/domain/test_errors.py
git commit -m "feat(errors): add authorization_denied category (#23)"
git log -1 --oneline
```

---

### Task 2: Add `destructive_ops` to the libvirt profile section

**Files:**
- Modify: `src/kdive/profiles/provisioning.py:LibvirtProfile`
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/profiles/test_provisioning.py`. The module already defines a `_valid()` helper (`return copy.deepcopy(_VALID)`) and imports `pytest` and `ProvisioningProfile` — reuse them; do **not** define a new helper:

```python
def test_destructive_ops_defaults_empty() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.destructive_ops == []


def test_destructive_ops_accepts_force_crash() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.destructive_ops == ["force_crash"]


def test_destructive_ops_rejects_blank_entry() -> None:
    from kdive.domain.errors import CategorizedError

    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = [" "]
    with pytest.raises(CategorizedError):
        ProvisioningProfile.parse(data)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -k destructive_ops -q`
Expected: FAIL — `destructive_ops` is rejected by `extra="forbid"` (the parse maps it to `CONFIGURATION_ERROR`, so the default/accept tests fail).

- [ ] **Step 3: Add the field**

In `src/kdive/profiles/provisioning.py`, in `LibvirtProfile`, add after `domain_xml_params`:

```python
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
```

Update the `LibvirtProfile` docstring to name `destructive_ops` as the opted-in destructive op kinds (default empty; deny-by-default for the gate, ADR-0028 §2).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -q`
Expected: PASS (the blank-entry test passes because `NonEmptyStr` strips+rejects `" "`).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py
git commit -m "feat(profiles): add libvirt destructive_ops opt-in field (#23)"
git log -1 --oneline
```

---

### Task 3: Extend `FakeDomain` with control methods

**Files:**
- Modify: `tests/providers/local_libvirt/conftest.py:FakeDomain`

This is test infrastructure for Tasks 4–6; no standalone test.

- [ ] **Step 1: Extend the fake**

In `tests/providers/local_libvirt/conftest.py`, add to `FakeDomain` (after `metadata`):

```python
    calls: list[str] = field(default_factory=list)
    raise_on: dict[str, int] = field(default_factory=dict)

    def _maybe_raise(self, op: str) -> None:
        code = self.raise_on.get(op)
        if code is not None:
            raise libvirt_error(code)

    def create(self) -> int:
        self.calls.append("create")
        self._maybe_raise("create")
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self._maybe_raise("destroy")
        return 0

    def reset(self, flags: int = 0) -> int:
        self.calls.append("reset")
        self._maybe_raise("reset")
        return 0

    def reboot(self, flags: int = 0) -> int:
        self.calls.append("reboot")
        self._maybe_raise("reboot")
        return 0

    def injectNMI(self, flags: int = 0) -> int:
        self.calls.append("injectNMI")
        self._maybe_raise("injectNMI")
        return 0
```

Add a `lookupByName` to `FakeLibvirtConn` so the control provider can resolve a domain:

```python
    lookup: dict[str, FakeDomain] = field(default_factory=dict)

    def lookupByName(self, name: str) -> FakeDomain:
        domain = self.lookup.get(name)
        if domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return domain
```

(`field` and `libvirt` are already imported in this conftest.)

- [ ] **Step 2: Verify nothing broke**

Run: `uv run python -m pytest tests/providers/local_libvirt -q`
Expected: PASS (existing discovery/provisioning tests still green; the new fields are additive defaults).

- [ ] **Step 3: Commit**

```bash
git add tests/providers/local_libvirt/conftest.py
git commit -m "test(control): extend FakeDomain with power/crash methods (#23)"
git log -1 --oneline
```

---

### Task 4: `PowerAction` enum + `Controller` Protocol + `LocalLibvirtControl.power`

**Files:**
- Create: `src/kdive/providers/local_libvirt/control.py`
- Test: `tests/providers/local_libvirt/test_control.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/local_libvirt/test_control.py`:

```python
"""LocalLibvirtControl provider tests — injected fake conn, no live host."""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.control import (
    LocalLibvirtControl,
    PowerAction,
)
from tests.providers.local_libvirt.conftest import FakeDomain, FakeLibvirtConn


def _control(domain: FakeDomain | None) -> tuple[LocalLibvirtControl, FakeDomain | None]:
    lookup = {domain.domain_name: domain} if domain is not None else {}
    conn = FakeLibvirtConn(lookup=lookup)
    return LocalLibvirtControl(connect=lambda: conn), domain


@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        (PowerAction.ON, "create"),
        (PowerAction.OFF, "destroy"),
        (PowerAction.RESET, "reset"),
        (PowerAction.CYCLE, "reboot"),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected_call: str) -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.power("kdive-x", action)
    assert domain is not None and domain.calls == [expected_call]


def test_power_on_already_running_swallowed() -> None:
    domain = FakeDomain(
        domain_name="kdive-x", system_id="x",
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.ON)  # no raise


def test_power_off_not_running_swallowed() -> None:
    domain = FakeDomain(
        domain_name="kdive-x", system_id="x",
        raise_on={"destroy": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    control, _ = _control(domain)
    control.power("kdive-x", PowerAction.OFF)  # no raise


def test_power_absent_domain_is_control_failure() -> None:
    control, _ = _control(None)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-gone", PowerAction.ON)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_power_other_libvirt_error_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x", system_id="x",
        raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.power("kdive-x", PowerAction.RESET)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_control.py -q`
Expected: FAIL — `kdive.providers.local_libvirt.lifecycle.control` does not exist.

- [ ] **Step 3: Write the provider**

Create `src/kdive/providers/local_libvirt/control.py`:

```python
"""Local-libvirt Control plane: power and force_crash a tagged domain (ADR-0028).

`LocalLibvirtControl` looks a domain up by name over an injected connection factory and
drives libvirt — `power(domain_name, action)` (`on→create`, `off→destroy`, `reset→reset`,
`cycle→reboot`) and `force_crash(domain_name)` (`injectNMI`). DB-free: it owns no Postgres;
the `control.*` handlers drive the state machine. The realized port keys on the libvirt
domain name (row-first ordering, ADR-0028 §1), distinct from the capability-dispatch
`ControlPlane` placeholder in `providers.interfaces`. Unit tests inject a fake connection;
the real `libvirt.open` adapter is `live_vm`-only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"


class PowerAction(StrEnum):
    """The four M0 power actions; a typed refinement of `interfaces.PowerAction` (str)."""

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def reset(self, flags: int) -> int: ...
    def reboot(self, flags: int) -> int: ...
    def injectNMI(self, flags: int) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]


class Controller(Protocol):
    """The handler-facing control port (the realized M0 contract), keyed on domain name."""

    def power(self, domain_name: str, action: PowerAction) -> None: ...
    def force_crash(self, domain_name: str) -> None: ...


def _close(conn: _LibvirtConn) -> None:
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtControl:
    """The `Controller` for the local libvirt host."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtControl:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        return cls(connect=lambda: libvirt.open(host_uri))  # ty: ignore[invalid-argument-type]

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Drive the domain's power state; idempotent on/off swallow the achieved post-state.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or any non-idempotent
                libvirt error occurs.
        """
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            self._apply_power(domain, domain_name, action)
        finally:
            _close(conn)

    def force_crash(self, domain_name: str) -> None:
        """Panic the guest via NMI (`injectNMI`).

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or libvirt errors.
        """
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            try:
                domain.injectNMI(0)
            except libvirt.libvirtError as exc:
                raise self._control_failure("injecting NMI into", domain_name) from exc
        finally:
            _close(conn)

    def _open(self) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._control_failure("connecting to libvirt for", "control") from exc

    def _lookup(self, conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._control_failure("looking up", domain_name) from exc

    def _apply_power(
        self, domain: _LibvirtDomain, domain_name: str, action: PowerAction
    ) -> None:
        try:
            if action is PowerAction.ON:
                self._idempotent(domain.create, "starting", domain_name)
            elif action is PowerAction.OFF:
                self._idempotent(domain.destroy, "stopping", domain_name)
            elif action is PowerAction.RESET:
                domain.reset(0)
            else:  # PowerAction.CYCLE
                domain.reboot(0)
        except libvirt.libvirtError as exc:
            raise self._control_failure(f"{action.value}-ing", domain_name) from exc

    @staticmethod
    def _idempotent(call: Callable[[], int], verb: str, domain_name: str) -> None:
        try:
            call()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise
            _log.info("%s domain %s: already in target state; treating as success", verb, domain_name)

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_control.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src
git add src/kdive/providers/local_libvirt/control.py tests/providers/local_libvirt/test_control.py
git commit -m "feat(control): LocalLibvirtControl power + force_crash provider (#23)"
git log -1 --oneline
```

---

### Task 5: `LocalLibvirtControl.force_crash` provider test (NMI)

**Files:**
- Test: `tests/providers/local_libvirt/test_control.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/local_libvirt/test_control.py`:

```python
def test_force_crash_injects_nmi() -> None:
    domain = FakeDomain(domain_name="kdive-x", system_id="x")
    control, domain = _control(domain)
    control.force_crash("kdive-x")
    assert domain is not None and domain.calls == ["injectNMI"]


def test_force_crash_absent_domain_is_control_failure() -> None:
    control, _ = _control(None)
    with pytest.raises(CategorizedError) as exc:
        control.force_crash("kdive-gone")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_force_crash_libvirt_error_is_control_failure() -> None:
    domain = FakeDomain(
        domain_name="kdive-x", system_id="x",
        raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    control, _ = _control(domain)
    with pytest.raises(CategorizedError) as exc:
        control.force_crash("kdive-x")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_control.py -k force_crash -q`
Expected: PASS (Task 4 already implemented `force_crash`).

- [ ] **Step 3: Commit**

```bash
git add tests/providers/local_libvirt/test_control.py
git commit -m "test(control): pin force_crash NMI provider behavior (#23)"
git log -1 --oneline
```

---

### Task 6: `control.power` tool + handler

**Files:**
- Create: `src/kdive/mcp/tools/control.py`
- Test: `tests/mcp/test_control_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_control_tools.py` (reuse the `_pool`/`_granted_allocation`/`_seed_system` helpers from `tests/mcp/test_systems_tools.py` by copying them in, plus a `_FakeControl`):

```python
"""control.* tool + handler tests — handlers called directly with injected pool + control."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.state import AllocationState, SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import control as control_tools
from kdive.providers.local_libvirt.lifecycle.control import PowerAction
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import Role
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            "crashkernel": "256M",
        }
    },
}


def _profile(*, destructive_ops: list[str] | None = None) -> dict[str, Any]:
    data = copy.deepcopy(_PROFILE)
    if destructive_ops is not None:
        data["provider"]["local-libvirt"]["destructive_ops"] = destructive_ops
    return data


def _ctx(role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _granted_allocation(pool: AsyncConnectionPool, *, scope: dict[str, Any] | None = None) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(conn, disc, pool="local-libvirt", cost_class="local")
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                resource_id=res.id, state=AllocationState.GRANTED, capability_scope=scope or {},
            ),
        )
    return str(alloc.id)


async def _seed_system(
    pool: AsyncConnectionPool, alloc_id: str, state: SystemState,
    *, destructive_ops: list[str] | None = None, domain_name: str | None = None,
) -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                allocation_id=UUID(alloc_id), state=state,
                provisioning_profile=_profile(destructive_ops=destructive_ops),
                domain_name=domain_name,
            ),
        )
    return str(system.id)


class _FakeControl:
    def __init__(self) -> None:
        self.powered: list[tuple[str, str]] = []
        self.crashed: list[str] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.powered.append((domain_name, action.value))

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)


# --- control.power tool -------------------------------------------------------------------


def test_power_ready_system_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await control_tools.power_system(pool, _ctx(), system_id=sys_id, action="off")
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'power' AND dedup_key LIKE %s",
                    (f"{sys_id}:power:off:%",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_power_unknown_action_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await control_tools.power_system(pool, _ctx(), system_id=sys_id, action="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_power_non_started_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.DEFINED)
            resp = await control_tools.power_system(pool, _ctx(), system_id=sys_id, action="off")
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "defined"

    asyncio.run(_run())


def test_power_without_operator_raises(migrated_url: str) -> None:
    from kdive.security.rbac import AuthorizationError

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await control_tools.power_system(pool, _ctx(Role.VIEWER), system_id=sys_id, action="off")

    asyncio.run(_run())


def test_power_handler_calls_provider_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn, JobKind.POWER, {"system_id": sys_id, "action": "reset"},
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{sys_id}:power:reset:{uuid4()}",
                )
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_tools.power_handler(conn, job, ctrl)
            assert ctrl.powered == [("kdive-x", "reset")]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s AND transition = 'power:reset'",
                    (sys_id,),
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "ready"  # no state move
        assert audit_row is not None and audit_row["n"] == 1

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k power -q`
Expected: FAIL — `kdive.mcp.tools.control` does not exist.

- [ ] **Step 3: Write `control.py` (power half)**

Create `src/kdive/mcp/tools/control.py`. Mirror `systems.py`'s helpers (`_config_error`, `_as_uuid`, `_authorizing`, `_ctx_from_job`, `_system_job_envelope`, `_audit_transition` adapted to `tool="control.power"`). Implement:

```python
"""The `control.*` MCP tools and the power/force_crash job handlers (ADR-0028).

`control.power` (ungated, operator) and `control.force_crash` (three-check gated, admin)
admit synchronously and enqueue a job; the handlers drive the domain via the injected
`Controller` under the per-System advisory lock. `power` moves no System state; `force_crash`
drives System `ready→crashed` and any DebugSession `live→detached`.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.control import Controller, LocalLibvirtControl, PowerAction
from kdive.providers.local_libvirt.lifecycle.provisioning import domain_name_for
from kdive.security import audit
from kdive.security.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

_STARTED_SYSTEM = frozenset({SystemState.READY, SystemState.CRASHED})
_FORCE_CRASH = "force_crash"


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _authorizing(ctx: RequestContext, project: str) -> dict[str, Any]:
    return {"principal": ctx.principal, "agent_session": ctx.agent_session, "project": project}


def _ctx_from_job(job: Job, project: str) -> RequestContext:
    auth = job.authorizing
    agent_session: str | None = auth.get("agent_session")
    return RequestContext(
        principal=str(auth["principal"]), agent_session=agent_session, projects=(project,), roles={}
    )


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, "system_id": str(system_id)}})


def _domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


async def power_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str, action: str
) -> ToolResponse:
    """Admit a power op on a started System and enqueue a `power` job (operator, ungated)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    try:
        power_action = PowerAction(action)
    except ValueError:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            if system.state not in _STARTED_SYSTEM:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn, JobKind.POWER, {"system_id": system_id, "action": power_action.value},
                _authorizing(ctx, system.project), f"{system_id}:power:{power_action.value}:{uuid4()}",
            )
        return _system_job_envelope(job, uid)


async def power_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Drive the domain's power; audit `power:{action}`; move no System state (ADR-0028 §3)."""
    system_id = UUID(job.payload["system_id"])
    action = PowerAction(job.payload["action"])
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone", category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        control.power(_domain_name(system), action)
        await audit.record(
            conn, _ctx_from_job(job, system.project), tool="control.power", object_kind="systems",
            object_id=system_id, transition=f"power:{action.value}",
            args={"system_id": str(system_id), "action": action.value}, project=system.project,
        )
    return str(system_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k power -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src
git add src/kdive/mcp/tools/control.py tests/mcp/test_control_tools.py
git commit -m "feat(control): control.power tool + handler (#23)"
git log -1 --oneline
```

---

### Task 7: `control.force_crash` tool (gate + admission)

**Files:**
- Modify: `src/kdive/mcp/tools/control.py`
- Test: `tests/mcp/test_control_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_control_tools.py`:

```python
def _admin(scope_ok: bool = True, opt_in: bool = True) -> tuple[dict[str, Any], list[str]]:
    scope = {"destructive_ops": ["force_crash"]} if scope_ok else {}
    ops = ["force_crash"] if opt_in else []
    return scope, ops


async def _crash(pool: AsyncConnectionPool, ctx: RequestContext, sys_id: str):
    return await control_tools.force_crash_system(pool, ctx, system_id=sys_id)


def _admin_ctx() -> RequestContext:
    return RequestContext(principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.ADMIN})


@pytest.mark.parametrize(
    ("scope_ok", "is_admin", "opt_in", "missing"),
    [
        (False, True, True, "capability_scope"),
        (True, False, True, "admin_role"),
        (True, True, False, "profile_opt_in"),
    ],
)
def test_force_crash_denied_returns_authorization_denied(
    migrated_url: str, scope_ok: bool, is_admin: bool, opt_in: bool, missing: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            scope, ops = _admin(scope_ok, opt_in)
            alloc_id = await _granted_allocation(pool, scope=scope)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, destructive_ops=ops)
            ctx = _admin_ctx() if is_admin else RequestContext(
                principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
            )
            resp = await _crash(pool, ctx, sys_id)
            assert resp.status == "error" and resp.error_category == "authorization_denied"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s AND transition = 'force_crash:denied'",
                    (sys_id,),
                )
                row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'force_crash'")
                jobs_row = await cur.fetchone()
        assert row is not None and row["n"] == 1
        assert jobs_row is not None and jobs_row["n"] == 0  # no job on denial

    asyncio.run(_run())


def test_force_crash_allowed_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool, scope={"destructive_ops": ["force_crash"]})
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, destructive_ops=["force_crash"])
            resp = await _crash(pool, _admin_ctx(), sys_id)
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:force_crash",)
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_force_crash_non_ready_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool, scope={"destructive_ops": ["force_crash"]})
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED, destructive_ops=["force_crash"])
            resp = await _crash(pool, _admin_ctx(), sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "crashed"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k force_crash -q`
Expected: FAIL — `force_crash_system` does not exist.

- [ ] **Step 3: Add the tool to `control.py`**

Append to `src/kdive/mcp/tools/control.py`:

```python
def _opt_in(system: System) -> bool:
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    return _FORCE_CRASH in profile.provider.local_libvirt.destructive_ops


async def force_crash_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Gate, admit, and enqueue a `force_crash` job for a `ready` System (admin + gate)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            allocation = await ALLOCATIONS.get(conn, system.allocation_id)
            if allocation is None or allocation.project not in ctx.projects:
                return _config_error(system_id)
            op = DestructiveOp(kind=_FORCE_CRASH, profile_opt_in=_opt_in(system))
            try:
                assert_destructive_allowed(ctx, allocation, op)
            except DestructiveOpDenied as denied:
                async with conn.transaction():
                    await audit.record(
                        conn, ctx, tool="control.force_crash", object_kind="systems",
                        object_id=uid, transition="force_crash:denied",
                        args={"system_id": system_id, "missing": denied.missing}, project=system.project,
                    )
                return ToolResponse.failure(system_id, ErrorCategory.AUTHORIZATION_DENIED)
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn, JobKind.FORCE_CRASH, {"system_id": system_id},
                _authorizing(ctx, system.project), f"{system_id}:force_crash",
            )
        return _system_job_envelope(job, uid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k force_crash -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src
git add src/kdive/mcp/tools/control.py tests/mcp/test_control_tools.py
git commit -m "feat(control): force_crash gate + admission tool (#23)"
git log -1 --oneline
```

---

### Task 8: `control.force_crash` handler (System + DebugSession transitions)

**Files:**
- Modify: `src/kdive/mcp/tools/control.py`
- Test: `tests/mcp/test_control_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_control_tools.py` (helpers seed a Run + DebugSession so the join detaches it). **First** move these imports up into the module's existing top-of-file import block (ruff `E402` forbids imports after code), merging into the existing `from kdive.domain.models import …` / `from kdive.domain.state import …` / `from kdive.db.repositories import …` lines rather than adding mid-file:

```python
# (merge into the existing top-of-file imports)
from kdive.db.repositories import DEBUG_SESSIONS, INVESTIGATIONS, RUNS  # + ALLOCATIONS, SYSTEMS
from kdive.domain.models import DebugSession, Investigation, Run  # + Allocation, Job, JobKind, System
from kdive.domain.state import DebugSessionState, InvestigationState, RunState  # + AllocationState, SystemState
```

Then append the helpers and tests:

```python
async def _seed_live_session(pool: AsyncConnectionPool, sys_id: str) -> str:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn, Investigation(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                title="t", state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn, Run(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                investigation_id=inv.id, system_id=UUID(sys_id), state=RunState.RUNNING, build_profile={},
            ),
        )
        session = await DEBUG_SESSIONS.insert(
            conn, DebugSession(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                run_id=run.id, state=DebugSessionState.LIVE, transport="gdbstub",
            ),
        )
    return str(session.id)


async def _enqueue_crash(pool: AsyncConnectionPool, sys_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn, JobKind.FORCE_CRASH, {"system_id": sys_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"}, f"{sys_id}:force_crash",
        )


def test_force_crash_handler_crashes_and_detaches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            session_id = await _seed_live_session(pool, sys_id)
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_tools.force_crash_handler(conn, job, ctrl)
            assert ctrl.crashed == ["kdive-x"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM debug_sessions WHERE id = %s", (session_id,))
                sess_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "crashed"
        assert sess_row is not None and sess_row["state"] == "detached"

    asyncio.run(_run())


def test_force_crash_handler_no_session_is_noop_detach(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY, domain_name="kdive-x")
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_tools.force_crash_handler(conn, job, ctrl)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "crashed"

    asyncio.run(_run())


def test_force_crash_handler_already_crashed_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED, domain_name="kdive-x")
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_tools.force_crash_handler(conn, job, ctrl)  # no raise
            assert ctrl.crashed == ["kdive-x"]  # NMI re-attempted
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'ready->crashed'"
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0  # no transition audited on the idempotent re-run

    asyncio.run(_run())


def test_force_crash_handler_terminal_system_does_not_crash(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN, domain_name="kdive-x")
            job = await _enqueue_crash(pool, sys_id)
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                await control_tools.force_crash_handler(conn, job, ctrl)
            assert ctrl.crashed == []  # teardown won the race; no NMI

    asyncio.run(_run())


def test_force_crash_handler_missing_system_is_infra_failure(migrated_url: str) -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job = await _enqueue_crash(pool, str(uuid4()))
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await control_tools.force_crash_handler(conn, job, ctrl)
        assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k handler -q`
Expected: FAIL — `force_crash_handler` does not exist.

- [ ] **Step 3: Add the handler to `control.py`**

Append to `src/kdive/mcp/tools/control.py`:

```python
_TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})


async def force_crash_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Crash the guest and drive System ready→crashed + DebugSession live→detached.

    Re-reads System state under the per-System lock (the admission `ready` check is advisory).
    A terminal System (teardown won the race) skips the NMI and any transition; an
    already-`crashed` System re-attempts the idempotent NMI but makes no transition.
    """
    system_id = UUID(job.payload["system_id"])
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone", category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in _TERMINAL_SYSTEM:
            return str(system_id)  # teardown/failure won the race; nothing to crash
        control.force_crash(_domain_name(system))
        if system.state is SystemState.READY:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn, _ctx_from_job(job, system.project), tool="control.force_crash",
                object_kind="systems", object_id=system_id, transition="ready->crashed",
                args={"system_id": str(system_id)}, project=system.project,
            )
        await _detach_sessions(conn, job, system)
    return str(system_id)


async def _detach_sessions(conn: AsyncConnection, job: Job, system: System) -> None:
    """Drive every non-terminal DebugSession of `system` to detached (join through runs)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = 'detached' "
            "WHERE state IN ('attach', 'live') "
            "  AND run_id IN (SELECT id FROM runs WHERE system_id = %s) "
            "RETURNING id, state",
            (system.id,),
        )
        rows = await cur.fetchall()
    for session_id, _old in rows:
        await audit.record(
            conn, _ctx_from_job(job, system.project), tool="control.force_crash",
            object_kind="debug_sessions", object_id=session_id, transition="live->detached",
            args={"system_id": str(system.id)}, project=system.project,
        )
```

Note: `SYSTEMS.update_state` opens its own nested transaction (savepoint) — safe inside the lock-holding `conn.transaction()`. The `_detach_sessions` UPDATE runs raw because it spans rows the repository layer has no per-System method for.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py -k handler -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src
git add src/kdive/mcp/tools/control.py tests/mcp/test_control_tools.py
git commit -m "feat(control): force_crash handler drives System+DebugSession (#23)"
git log -1 --oneline
```

---

### Task 9: Register the plane + handlers

**Files:**
- Modify: `src/kdive/mcp/tools/control.py` (add `register`/`register_handlers`)
- Modify: `src/kdive/mcp/app.py`
- Test: `tests/mcp/test_control_tools.py`, `tests/mcp/test_app.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_control_tools.py`:

```python
def test_register_handlers_binds_power_and_force_crash() -> None:
    registry = HandlerRegistry()
    control_tools.register_handlers(registry, control=_FakeControl())
    assert registry.get(JobKind.POWER) is not None
    assert registry.get(JobKind.FORCE_CRASH) is not None
```

In `tests/mcp/test_app.py`, the `test_build_app_registers_jobs_tools` test enumerates `names = {t.name for t in await app.list_tools()}` and makes subset assertions like `assert {"systems.provision", …} <= names`. Add one more assertion line in that test (do **not** edit an existing set):

```python
        assert {"control.power", "control.force_crash"} <= names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py::test_register_handlers_binds_power_and_force_crash tests/mcp/test_app.py -q`
Expected: FAIL — `register_handlers` missing and `control.*` tools absent from the app.

- [ ] **Step 3: Add registration to `control.py`**

Append to `src/kdive/mcp/tools/control.py`:

```python
def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `control.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="control.power")
    async def control_power(system_id: str, action: str) -> ToolResponse:
        return await power_system(pool, current_context(), system_id=system_id, action=action)

    @app.tool(name="control.force_crash")
    async def control_force_crash(system_id: str) -> ToolResponse:
        return await force_crash_system(pool, current_context(), system_id=system_id)


def register_handlers(registry: HandlerRegistry, *, control: Controller | None = None) -> None:
    """Bind the `power`/`force_crash` job handlers; build the provider lazily from env."""
    ctrl = control or LocalLibvirtControl.from_env()

    async def _power(conn: AsyncConnection, job: Job) -> str | None:
        return await power_handler(conn, job, ctrl)

    async def _force_crash(conn: AsyncConnection, job: Job) -> str | None:
        return await force_crash_handler(conn, job, ctrl)

    registry.register(JobKind.POWER, _power)
    registry.register(JobKind.FORCE_CRASH, _force_crash)
```

- [ ] **Step 4: Wire `app.py`**

In `src/kdive/mcp/app.py`: add `control` to the `from kdive.mcp.tools import …` line, append `control.register` to `_PLANE_REGISTRARS`, and append `control.register_handlers` to `_HANDLER_REGISTRARS`. Update the `_HANDLER_REGISTRARS` comment to mention the control plane.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_control_tools.py tests/mcp/test_app.py -q`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check src
git add src/kdive/mcp/tools/control.py src/kdive/mcp/app.py tests/mcp/test_control_tools.py tests/mcp/test_app.py
git commit -m "feat(control): register control plane tools + handlers (#23)"
git log -1 --oneline
```

---

### Task 10: Full-suite green + final guardrails

- [ ] **Step 1: Run the whole suite + guardrails**

```bash
uv run ruff check && uv run ruff format --check && uv run ty check src && uv run python -m pytest -q
```
Expected: all PASS, zero warnings. The `live_vm` end-to-end crash test is gated and skipped in CI (expected).

- [ ] **Step 2: If anything is red, fix and re-commit**

Address any ty/ruff/test failure; re-run; commit the fix. Verify `git log -1 --oneline` after each.

---

## Self-review notes (spec coverage)

- Spec "Controller port + LocalLibvirtControl" → Tasks 4, 5.
- Spec "control.power tool + handler" → Task 6 (incl. power-off domain note: handler moves no state).
- Spec "control.force_crash tool" (gate, opt-in, denial audit, ready-only) → Task 7.
- Spec "control.force_crash handler" (FOR UPDATE re-read via SYSTEMS.get under lock; terminal/crashed/ready branches; NMI-before-transition; detach join-through-runs) → Task 8.
- Spec plumbing (`destructive_ops`, `AUTHORIZATION_DENIED`, registrars) → Tasks 1, 2, 9.
- Spec failure contract rows → Tasks 6–8 edge tests.
- Spec `live_vm` (gated) → not implemented as a CI test (no host); the gate-marker live test is a follow-up an operator runs, consistent with the provisioning plane's `live_vm`-only end-to-end.

**Note on the force_crash handler lock re-read:** the spec describes `SELECT … FOR UPDATE`; `SYSTEMS.get` is a plain `SELECT`, but it runs inside the per-System advisory lock which already serializes every System mutation in this codebase (teardown/provision all mutate under the same `LockScope.SYSTEM` lock), so the advisory lock provides the mutual exclusion the `FOR UPDATE` row lock would. If a reviewer insists on row-level `FOR UPDATE`, replace `SYSTEMS.get` with a `SELECT * FROM systems WHERE id = %s FOR UPDATE` cursor read (the systems.py provision-handler pattern) — both are correct under the advisory lock.
