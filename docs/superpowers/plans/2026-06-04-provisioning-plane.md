# Provisioning plane (libvirt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision a `granted` Allocation into a running, kdive-tagged libvirt System and tear it down, as `systems.*` tools backed by `provision`/`teardown` job handlers.

**Architecture:** A pure-libvirt provider (`LocalLibvirtProvisioning`) renders+defines/destroys a tagged domain over an injected connection factory. `systems.provision` synchronously mints a System (`provisioning`) under a per-allocation lock, flips the Allocation `granted → active`, and enqueues a job; the handlers do the libvirt work and drive the System state machine, serializing on a per-System lock so a release-mid-provision cannot leak a domain.

**Tech Stack:** Python 3.13 · `psycopg` 3 (async) · `libvirt-python` · Pydantic v2 · FastMCP 3.x · `xml.etree.ElementTree` · `pytest` (testcontainers Postgres) · `ruff`/`ty`.

**Design source:** [`../specs/2026-06-04-provisioning-plane-design.md`](../specs/2026-06-04-provisioning-plane-design.md) · [`../../adr/0025-provisioning-plane-libvirt.md`](../../adr/0025-provisioning-plane-libvirt.md). The spec's "Failure modes & edges" section is the authoritative test list; each task below implements its slice and references it for the full edge enumeration.

---

## File structure

- **Create** `src/kdive/providers/local_libvirt/provisioning.py` — the `ProvisioningPlane`: `domain_name_for`, `SUPPORTED_DOMAIN_XML_PARAMS`, `validate_profile`, `render_domain_xml`, `LocalLibvirtProvisioning.{from_env,provision,teardown}`. DB-free.
- **Create** `src/kdive/mcp/tools/systems.py` — `systems.*` tools (`provision`/`get`/`teardown`), the `provision`/`teardown` job handlers, `register`, `register_handlers`.
- **Create** `tests/providers/local_libvirt/test_provisioning.py` — provider unit tests (fake libvirt conn).
- **Create** `tests/mcp/test_systems_tools.py` — tool + handler tests (real Postgres, fake provider double).
- **Modify** `src/kdive/domain/state.py:133` — add `SystemState.PROVISIONING → TORN_DOWN`.
- **Modify** `tests/domain/test_state.py:50` — add the same edge to the `LEGAL` table.
- **Modify** `src/kdive/mcp/app.py:24-33` — append `systems.register` / `systems.register_handlers` to the two registrar tuples.

---

## Task 1: Add the `provisioning → torn_down` state edge

**Files:**
- Modify: `src/kdive/domain/state.py:131-140`
- Modify: `tests/domain/test_state.py:48-55`

- [ ] **Step 1: Update the test's LEGAL table (the spec mirror) first**

In `tests/domain/test_state.py`, change the `SystemState.PROVISIONING` entry (line 50):

```python
    SystemState: {
        SystemState.DEFINED: {SystemState.PROVISIONING, SystemState.FAILED},
        SystemState.PROVISIONING: {
            SystemState.READY,
            SystemState.FAILED,
            SystemState.TORN_DOWN,
        },
        SystemState.READY: {SystemState.CRASHED, SystemState.TORN_DOWN, SystemState.FAILED},
        SystemState.CRASHED: {SystemState.TORN_DOWN, SystemState.FAILED},
        SystemState.TORN_DOWN: set(),
        SystemState.FAILED: set(),
    },
```

- [ ] **Step 2: Run the state tests to verify the new edge fails as illegal**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: FAIL — the parametrized `test_legal_transitions_are_allowed[TORN_DOWN]` (provisioning→torn_down) fails because the implementation table still rejects it.

- [ ] **Step 3: Add the edge to the implementation guard table**

In `src/kdive/domain/state.py`, change the `SystemState.PROVISIONING` line (133):

```python
        SystemState.PROVISIONING: frozenset(
            {SystemState.READY, SystemState.FAILED, SystemState.TORN_DOWN}
        ),
```

- [ ] **Step 4: Run the state tests to verify they pass**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: PASS (the legal-edge param now passes; the illegal-edge generator no longer yields it).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/state.py tests/domain/test_state.py
git commit -m "feat(domain): add System provisioning->torn_down edge (#16)"
```

---

## Task 2: Provider — `domain_name_for`, `validate_profile`, `render_domain_xml`

**Files:**
- Create: `src/kdive/providers/local_libvirt/provisioning.py`
- Create: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] **Step 1: Write the failing rendering tests**

Create `tests/providers/local_libvirt/test_provisioning.py`:

```python
"""Tests for the local-libvirt Provisioning plane (ADR-0025)."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt import discovery
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    domain_name_for,
    render_domain_xml,
    validate_profile,
)
from tests.providers.local_libvirt.conftest import libvirt_error

_SYS = UUID("11111111-1111-1111-1111-111111111111")

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            "crashkernel": "256M",
        }
    },
}


def _profile(**overrides: Any) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID)
    data["provider"]["local-libvirt"].update(overrides)
    return ProvisioningProfile.parse(data)


def test_domain_name_is_kdive_prefixed() -> None:
    assert domain_name_for(_SYS) == "kdive-11111111-1111-1111-1111-111111111111"


def test_render_carries_name_memory_vcpu_machine_and_rootfs() -> None:
    root = ET.fromstring(render_domain_xml(_SYS, _profile()))
    assert root.findtext("name") == "kdive-11111111-1111-1111-1111-111111111111"
    assert root.findtext("memory") == "4096"
    assert root.findtext("vcpu") == "4"
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "x86_64"
    assert os_type.get("machine") == "pc-q35-9.0"
    source = root.find("devices/disk/source")
    assert source is not None
    assert source.get("file") == "oci://registry.internal/rootfs/fedora-40@sha256:abc123"


def test_render_has_no_kernel_or_cmdline() -> None:
    # The kdump crashkernel reservation is the install/boot plane's job (#17), not provision's.
    root = ET.fromstring(render_domain_xml(_SYS, _profile()))
    assert root.find("os/kernel") is None
    assert root.find("os/cmdline") is None


def test_render_metadata_tag_round_trips_through_discovery() -> None:
    root = ET.fromstring(render_domain_xml(_SYS, _profile()))
    tag = root.find(f"metadata/{{{discovery._KDIVE_METADATA_NS}}}system")
    assert tag is not None
    assert discovery._parse_system_id(ET.tostring(tag, encoding="unicode")) == str(_SYS)


def test_render_defaults_machine_when_absent() -> None:
    root = ET.fromstring(render_domain_xml(_SYS, _profile(domain_xml_params={})))
    os_type = root.find("os/type")
    assert os_type is not None and os_type.get("machine") == "q35"


def test_validate_profile_rejects_unknown_domain_xml_param() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_profile(_profile(domain_xml_params={"machine": "q35", "bogus": "x"}))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_render_rejects_unknown_domain_xml_param() -> None:
    # render re-checks at the worker boundary (a hand-built jsonb that bypassed the tool).
    with pytest.raises(CategorizedError):
        render_domain_xml(_SYS, _profile(domain_xml_params={"nope": "x"}))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.local_libvirt.provisioning`.

- [ ] **Step 3: Write the rendering implementation**

Create `src/kdive/providers/local_libvirt/provisioning.py`:

```python
"""Local-libvirt Provisioning plane: define/start and destroy/undefine a tagged domain (ADR-0025).

`LocalLibvirtProvisioning` renders a domain XML from a `ProvisioningProfile` (tagged with the
System id in the kdive metadata element discovery reads), `defineXML`+`create`s it on
`provision`, and `destroy`+`undefine`s it idempotently on `teardown`, over an injected
connection factory (unit tests never touch a real host; the real `libvirt.open` adapter is
`live_vm`-only). It owns no Postgres — the `systems.*` handlers drive the state machine.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.discovery import _KDIVE_METADATA_NS

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_DEFAULT_MACHINE = "q35"
SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _LibvirtConn(Protocol):
    def defineXML(self, xml: str) -> _LibvirtDomain: ...
    def lookupByName(self, name: str) -> _LibvirtDomain: ...


type Connect = Callable[[], _LibvirtConn]


def domain_name_for(system_id: UUID) -> str:
    """The deterministic libvirt domain name for a System."""
    return f"kdive-{system_id}"


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject a profile whose libvirt ``domain_xml_params`` carry an unsupported key.

    Called at the tool boundary so a bad param is a synchronous ``configuration_error``
    response, not a dead-lettered provision job.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the unsupported key(s).
    """
    params = profile.provider.local_libvirt.domain_xml_params
    unknown = sorted(set(params) - SUPPORTED_DOMAIN_XML_PARAMS)
    if unknown:
        raise CategorizedError(
            f"unsupported domain_xml_params: {', '.join(unknown)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
        )


def render_domain_xml(system_id: UUID, profile: ProvisioningProfile) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    Built with ``ElementTree`` (no string interpolation), so a profile value cannot inject
    XML. Renders the domain shell, the rootfs disk, and the kdive metadata tag — **no**
    ``<kernel>``/``<cmdline>`` (the kdump ``crashkernel=`` reservation is the install/boot
    plane's, #17, and is inert without a ``<kernel>`` element).
    """
    validate_profile(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "source", file=section.rootfs_image_ref)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{_KDIVE_METADATA_NS}}}system").text = str(system_id)

    ET.register_namespace("kdive", _KDIVE_METADATA_NS)
    return ET.tostring(domain, encoding="unicode")
```

- [ ] **Step 4: Run to verify the rendering tests pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: PASS (the `LocalLibvirtProvisioning` import resolves; only rendering/validate tests exist so far).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/provisioning.py tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(provisioning): render tagged libvirt domain XML; validate params (#16)"
```

---

## Task 3: Provider — `LocalLibvirtProvisioning.provision` / `.teardown` / `from_env`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/provisioning.py`
- Modify: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] **Step 1: Write the failing provider tests**

Append to `tests/providers/local_libvirt/test_provisioning.py` (the fakes + tests):

```python
@dataclass
class _ProvDomain:
    domain_name: str
    created: bool = False
    destroyed: bool = False
    undefined: bool = False
    create_error: int | None = None
    destroy_error: int | None = None
    undefine_error: int | None = None

    def create(self) -> int:
        if self.create_error is not None:
            raise libvirt_error(self.create_error)
        self.created = True
        return 0

    def destroy(self) -> int:
        if self.destroy_error is not None:
            raise libvirt_error(self.destroy_error)
        self.destroyed = True
        return 0

    def undefine(self) -> int:
        if self.undefine_error is not None:
            raise libvirt_error(self.undefine_error)
        self.undefined = True
        return 0


@dataclass
class _ProvConn:
    defined: dict[str, _ProvDomain] = field(default_factory=dict)
    define_error: int | None = None
    lookup_error: int | None = None  # raised by lookupByName (e.g. NO_DOMAIN)

    def defineXML(self, xml: str) -> _ProvDomain:
        if self.define_error is not None:
            raise libvirt_error(self.define_error)
        name = ET.fromstring(xml).findtext("name")
        assert name is not None
        dom = self.defined.setdefault(name, _ProvDomain(name))
        return dom

    def lookupByName(self, name: str) -> _ProvDomain:
        if self.lookup_error is not None:
            raise libvirt_error(self.lookup_error)
        if name not in self.defined:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.defined[name]


def _prov(conn: _ProvConn) -> LocalLibvirtProvisioning:
    return LocalLibvirtProvisioning(connect=lambda: conn)


def test_provision_defines_and_starts_returns_name() -> None:
    conn = _ProvConn()
    name = _prov(conn).provision(_SYS, _profile())
    assert name == "kdive-11111111-1111-1111-1111-111111111111"
    assert conn.defined[name].created is True


def test_provision_define_error_is_provisioning_failure() -> None:
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_create_error_is_provisioning_failure() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_teardown_destroys_and_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.destroyed is True and dom.undefined is True


def test_teardown_absent_domain_is_noop() -> None:
    _prov(_ProvConn()).teardown(domain_name_for(_SYS))  # no raise


def test_teardown_not_running_domain_still_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, destroy_error=libvirt.VIR_ERR_OPERATION_INVALID)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.undefined is True  # OPERATION_INVALID on destroy is ignored


def test_teardown_other_libvirt_error_is_infrastructure_failure() -> None:
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).teardown(domain_name_for(_SYS))
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    prov = LocalLibvirtProvisioning.from_env()  # building must not open a connection
    assert isinstance(prov, LocalLibvirtProvisioning)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: FAIL with `AttributeError: 'LocalLibvirtProvisioning'` (class/methods not defined yet).

- [ ] **Step 3: Implement the provider class**

Append to `src/kdive/providers/local_libvirt/provisioning.py`:

```python
class LocalLibvirtProvisioning:
    """The `ProvisioningPlane` for the local libvirt host (define/start, destroy/undefine)."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        # libvirt ships no type stubs matching `_LibvirtConn`; duck-typed at the seam.
        return cls(connect=lambda: libvirt.open(host_uri))  # ty: ignore[invalid-argument-type]

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Define and start the tagged domain; return its name.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` on any libvirt error.
        """
        xml = render_domain_xml(system_id, profile)
        try:
            domain = self._connect().defineXML(xml)
            domain.create()
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define/start the domain",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        return domain_name_for(system_id)

    def teardown(self, domain_name: str) -> None:
        """Destroy and undefine the domain; idempotent over an already-absent domain.

        "No such domain" on lookup/undefine and "not running" on destroy are the achieved
        post-state, so they are swallowed; any other libvirt error fails.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any other libvirt error.
        """
        conn = self._connect()
        try:
            domain = conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return
            raise self._infra("looking up", domain_name) from exc
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise self._infra("destroying", domain_name) from exc
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise self._infra("undefining", domain_name) from exc

    @staticmethod
    def _infra(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        )
```

- [ ] **Step 4: Run to verify the provider tests pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q && uv run ty check src`
Expected: PASS; ty clean (the `libvirt.open` seam carries the scoped ignore).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/provisioning.py tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(provisioning): define/start and idempotent destroy/undefine (#16)"
```

---

## Task 4: `systems.py` skeleton + `systems.get`

**Files:**
- Create: `src/kdive/mcp/tools/systems.py`
- Create: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing `systems.get` tests**

Create `tests/mcp/test_systems_tools.py`:

```python
"""systems.* tool + handler tests — handlers called directly with injected pool + provider."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.models import Allocation, System
from kdive.domain.state import AllocationState, SystemState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import systems as systems_tools
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


def _profile() -> dict[str, Any]:
    return copy.deepcopy(_PROFILE)


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


async def _granted_allocation(pool: AsyncConnectionPool, *, cap: int = 2) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=cap
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(conn, disc, pool="local-libvirt", cost_class="local")
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                resource_id=res.id, state=AllocationState.GRANTED,
            ),
        )
    return str(alloc.id)


async def _seed_system(pool: AsyncConnectionPool, alloc_id: str, state: SystemState) -> str:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
                allocation_id=UUID(alloc_id), state=state, provisioning_profile=_profile(),
            ),
        )
    return str(system.id)


def test_get_own_system_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await systems_tools.get_system(pool, _ctx(), sys_id)
        assert resp.object_id == sys_id
        assert resp.status == "ready"

    asyncio.run(_run())


def test_get_failed_system_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.FAILED)
            resp = await systems_tools.get_system(pool, _ctx(), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await systems_tools.get_system(pool, _ctx(projects=("other",)), sys_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await systems_tools.get_system(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.mcp.tools.systems`.

- [ ] **Step 3: Write the module skeleton + `get_system`**

Create `src/kdive/mcp/tools/systems.py`:

```python
"""The `systems.*` MCP tools and the provision/teardown job handlers (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation, flips the Allocation ``granted -> active``, and enqueues a ``provision`` job — all
atomic under a per-allocation advisory lock — then returns a job handle. The ``provision``
handler renders+defines the tagged libvirt domain and drives ``provisioning -> ready`` (or
``-> failed``); the ``teardown`` handler destroys+undefines and drives ``-> torn_down``. Both
serialize their state decision on a per-System lock so a release-mid-provision cannot leak a
domain. Handlers reconstruct a RequestContext from the job's authorizing tuple to audit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import AllocationState, IllegalTransition, SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    domain_name_for,
    validate_profile,
)
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

_TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_system(system: System) -> ToolResponse:
    """Render a System; ``failed`` becomes a failure envelope (its value is a failure status)."""
    if system.state is SystemState.FAILED:
        return ToolResponse.failure(
            str(system.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": system.state.value},
        )
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data={"project": system.project},
    )


async def get_system(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str) -> ToolResponse:
    """Return a System the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
        if system is None or system.project not in ctx.projects:
            return _config_error(system_id)
        return _envelope_for_system(system)
```

- [ ] **Step 4: Run to verify the `systems.get` tests pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -q`
Expected: PASS (the four `get_system` tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): systems.get tool + module skeleton (#16)"
```

---

## Task 5: `systems.provision` tool

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py`
- Modify: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing provision-tool tests**

Append to `tests/mcp/test_systems_tools.py`:

```python
async def _provision(pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: str, profile: dict[str, Any]):
    return await systems_tools.provision_system(pool, ctx, allocation_id=alloc_id, profile=profile)


def test_provision_mints_system_active_allocation_and_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
            assert resp.status == "queued"
            assert resp.data["system_id"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, allocation_id FROM systems")
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{alloc_id}:provision",))
                job_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "provisioning"
        assert str(sys_row["allocation_id"]) == alloc_id
        assert alloc_row is not None and alloc_row["state"] == "active"
        assert job_row is not None and job_row["n"] == 1

    asyncio.run(_run())


def test_provision_retry_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            first = await _provision(pool, _ctx(), alloc_id, _profile())
            second = await _provision(pool, _ctx(), alloc_id, _profile())
            assert first.object_id == second.object_id  # same job
            assert first.data["system_id"] == second.data["system_id"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'granted->active'"
                )
                audit_n = await cur.fetchone()
        assert sys_n is not None and sys_n["n"] == 1  # one System
        assert audit_n is not None and audit_n["n"] == 1  # active flip audited once

    asyncio.run(_run())


def test_provision_terminal_existing_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # Seed a torn_down System on the allocation; the allocation is spent (no reprovision).
            await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "torn_down"

    asyncio.run(_run())


def test_provision_non_granted_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
            resp = await _provision(pool, _ctx(), alloc_id, _profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "releasing"

    asyncio.run(_run())


def test_provision_unknown_domain_param_is_config_error_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            bad = _profile()
            bad["provider"]["local-libvirt"]["domain_xml_params"]["bogus"] = "x"
            resp = await _provision(pool, _ctx(), alloc_id, bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0  # validated before any write

    asyncio.run(_run())


def test_provision_without_operator_raises(migrated_url: str) -> None:
    from kdive.security.rbac import AuthorizationError

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(AuthorizationError):
                await _provision(pool, _ctx(Role.VIEWER), alloc_id, _profile())

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k provision -q`
Expected: FAIL with `AttributeError: module 'kdive.mcp.tools.systems' has no attribute 'provision_system'`.

- [ ] **Step 3: Implement `provision_system` + helpers**

Append to `src/kdive/mcp/tools/systems.py`:

```python
def _authorizing(ctx: RequestContext, project: str) -> dict[str, Any]:
    return {"principal": ctx.principal, "agent_session": ctx.agent_session, "project": project}


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    """A job-handle envelope (like `from_job`) carrying the System id in ``data``."""
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, "system_id": str(system_id)}})


async def _find_system_for_allocation(conn: AsyncConnection, alloc_id: UUID) -> System | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM systems WHERE allocation_id = %s ORDER BY created_at, id LIMIT 1",
            (alloc_id,),
        )
        row = await cur.fetchone()
    return System.model_validate(row) if row else None


async def provision_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    profile: dict[str, Any],
) -> ToolResponse:
    """Mint a System for a ``granted`` Allocation and enqueue its provision job."""
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
    except CategorizedError as exc:
        return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _provision_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)


async def _provision_locked(
    pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: UUID, profile: ProvisioningProfile
) -> ToolResponse:
    async with pool.connection() as conn:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id):
            alloc = await ALLOCATIONS.get(conn, alloc_id)
            if alloc is None or alloc.project not in ctx.projects:
                return _config_error(str(alloc_id))
            require_role(ctx, alloc.project, Role.OPERATOR)
            existing = await _find_system_for_allocation(conn, alloc_id)
            if existing is not None:
                if existing.state in _TERMINAL_SYSTEM:
                    return _config_error(
                        str(existing.id), data={"current_status": existing.state.value}
                    )
                job = await queue.enqueue(
                    conn, JobKind.PROVISION, {"system_id": str(existing.id)},
                    _authorizing(ctx, alloc.project), f"{alloc_id}:provision",
                )
                return _system_job_envelope(job, existing.id)
            if alloc.state is not AllocationState.GRANTED:
                return _config_error(str(alloc_id), data={"current_status": alloc.state.value})
            now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
            system = await SYSTEMS.insert(
                conn,
                System(
                    id=uuid4(), created_at=now, updated_at=now,
                    principal=ctx.principal, agent_session=ctx.agent_session, project=alloc.project,
                    allocation_id=alloc_id, state=SystemState.PROVISIONING,
                    provisioning_profile=profile.model_dump(by_alias=True),
                ),
            )
            await audit.record(
                conn, ctx, tool="systems.provision", object_kind="systems", object_id=system.id,
                transition="->provisioning", args={"allocation_id": str(alloc_id)},
                project=alloc.project,
            )
            await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.ACTIVE)
            await audit.record(
                conn, ctx, tool="systems.provision", object_kind="allocations", object_id=alloc_id,
                transition="granted->active", args={"allocation_id": str(alloc_id)},
                project=alloc.project,
            )
            job = await queue.enqueue(
                conn, JobKind.PROVISION, {"system_id": str(system.id)},
                _authorizing(ctx, alloc.project), f"{alloc_id}:provision",
            )
            return _system_job_envelope(job, system.id)
```

- [ ] **Step 4: Run to verify the provision-tool tests pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k provision -q`
Expected: PASS (all six provision tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): systems.provision mints System + enqueues job (#16)"
```

---

## Task 6: `provision` handler

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py`
- Modify: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing handler tests**

Append to `tests/mcp/test_systems_tools.py` (a recording fake provider + handler tests):

```python
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.jobs import queue


class _FakeProvisioning:
    """Records provision/teardown calls; provision returns a domain name or raises."""

    def __init__(self, *, provision_error: bool = False) -> None:
        self.provisioned: list[UUID] = []
        self.torn_down: list[str] = []
        self._provision_error = provision_error

    def provision(self, system_id: UUID, profile: Any) -> str:
        self.provisioned.append(system_id)
        if self._provision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)


async def _enqueue_provision(pool: AsyncConnectionPool, system_id: str, alloc_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn, JobKind.PROVISION, {"system_id": system_id},
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{alloc_id}:provision",
        )


def test_provision_handler_drives_system_ready(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                result = await systems_tools.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.provisioned == [UUID(sys_id)]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "ready"
        assert row["domain_name"] == f"kdive-{sys_id}"

    asyncio.run(_run())


def test_provision_handler_retry_on_ready_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.provision_handler(conn, job, prov)
            assert prov.provisioned == []  # already up; provider not called again

    asyncio.run(_run())


def test_provision_handler_provider_failure_sets_system_failed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning(provision_error=True)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await systems_tools.provision_handler(conn, job, prov)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "failed"
        assert row["domain_name"] is None

    asyncio.run(_run())


def test_provision_handler_superseded_by_teardown_cleans_up(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # Seed already torn_down: the provider creates the domain, but the finalize sees
            # a terminal System and tears the just-created domain down instead of -> ready.
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                result = await systems_tools.provision_handler(conn, job, prov)
        assert result == sys_id
        # torn_down is terminal, so step-2 returns before provisioning: provider not called.
        assert prov.provisioned == []

    asyncio.run(_run())


def test_provision_handler_missing_row_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            job = await _enqueue_provision(pool, str(uuid4()), alloc_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await systems_tools.provision_handler(conn, job, prov)
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())
```

> Note: the "superseded mid-flight" path (System driven terminal *between* the provider call and the locked finalize) is the rare two-worker race; the seeded-`torn_down` test above covers the step-2 terminal short-circuit. A dedicated mid-flight test is added in Task 8 once the teardown handler exists, by interleaving the two handlers.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k provision_handler -q`
Expected: FAIL — `provision_handler` not defined.

- [ ] **Step 3: Implement `provision_handler` + the audit helper**

Append to `src/kdive/mcp/tools/systems.py`:

```python
def _ctx_from_job(job: Job, project: str) -> RequestContext:
    """Reconstruct an attribution context from a job's authorizing tuple (ADR-0025 §9)."""
    auth = job.authorizing
    return RequestContext(
        principal=str(auth["principal"]),
        agent_session=auth.get("agent_session"),
        projects=(project,),
        roles={},
    )


async def _audit_transition(
    conn: AsyncConnection, job: Job, *, project: str, object_id: UUID, transition: str, tool: str
) -> None:
    await audit.record(
        conn, _ctx_from_job(job, project), tool=tool, object_kind="systems",
        object_id=object_id, transition=transition, args={"system_id": str(object_id)},
        project=project,
    )


async def provision_handler(
    conn: AsyncConnection, job: Job, provisioning: LocalLibvirtProvisioning
) -> str | None:
    """Define+start the tagged domain and drive the System ``provisioning -> ready``."""
    system_id = UUID(job.payload["system_id"])
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "provision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    if system.state is not SystemState.PROVISIONING:
        return str(system_id)  # ready (retry), or terminal (a teardown/failure raced ahead)
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    try:
        domain_name = provisioning.provision(system_id, profile)
    except CategorizedError:
        await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)
        await _audit_transition(
            conn, job, project=system.project, object_id=system_id,
            transition="provisioning->failed", tool="systems.provision",
        )
        raise
    superseded = True
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM systems WHERE id = %s FOR UPDATE", (system_id,))
            row = await cur.fetchone()
            if row is not None and SystemState(row["state"]) is SystemState.PROVISIONING:
                superseded = False
                await cur.execute(
                    "UPDATE systems SET state = %s, domain_name = %s WHERE id = %s",
                    (SystemState.READY.value, domain_name, system_id),
                )
        if not superseded:
            await _audit_transition(
                conn, job, project=system.project, object_id=system_id,
                transition="provisioning->ready", tool="systems.provision",
            )
    if superseded:
        # A concurrent teardown drove the System terminal; clean up the domain we created.
        provisioning.teardown(domain_name)
    return str(system_id)
```

- [ ] **Step 4: Run to verify the handler tests pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k provision_handler -q`
Expected: PASS (all five).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): provision handler drives provisioning->ready/failed (#16)"
```

---

## Task 7: `systems.teardown` tool + `teardown` handler

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py`
- Modify: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing teardown tests**

Append to `tests/mcp/test_systems_tools.py`:

```python
async def _teardown(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str):
    return await systems_tools.teardown_system(pool, ctx, system_id)


def test_teardown_tool_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(), sys_id)
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:teardown",))
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_teardown_tool_already_torn_down_no_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            resp = await _teardown(pool, _ctx(), sys_id)
            assert resp.status == "torn_down"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs")
                row = await cur.fetchone()
        assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_teardown_handler_destroys_and_sets_torn_down(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            # give it a domain_name so teardown uses the real one
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET domain_name = %s WHERE id = %s", (f"kdive-{sys_id}", sys_id)
                )
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.teardown_handler(conn, job, prov)
            assert prov.torn_down == [f"kdive-{sys_id}"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"

    asyncio.run(_run())


def test_teardown_handler_provisioning_system_one_transition(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.teardown_handler(conn, job, prov)
            # domain_name is NULL on a never-finalized provision -> deterministic name reaped
            assert prov.torn_down == [f"kdive-{sys_id}"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"  # provisioning->torn_down (one edge)

    asyncio.run(_run())


def test_teardown_handler_already_torn_down_is_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.TORN_DOWN)
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.teardown_handler(conn, job, prov)
            assert prov.torn_down == []  # provider not called

    asyncio.run(_run())


async def _enqueue_teardown(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn, JobKind.TEARDOWN, {"system_id": system_id},
            {"principal": "system:reconciler", "agent_session": None, "project": "proj"},
            f"{system_id}:teardown",
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k teardown -q`
Expected: FAIL — `teardown_system` / `teardown_handler` not defined.

- [ ] **Step 3: Implement `teardown_system` + `teardown_handler`**

Append to `src/kdive/mcp/tools/systems.py`:

```python
async def teardown_system(
    pool: AsyncConnectionPool, ctx: RequestContext, system_id: str
) -> ToolResponse:
    """Enqueue an idempotent teardown for a System the caller's project owns."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            if system.state is SystemState.TORN_DOWN:
                return ToolResponse.success(
                    system_id, "torn_down",
                    suggested_next_actions=["systems.get"], data={"project": system.project},
                )
            job = await queue.enqueue(
                conn, JobKind.TEARDOWN, {"system_id": str(uid)},
                _authorizing(ctx, system.project), f"{uid}:teardown",
            )
        return _system_job_envelope(job, uid)


async def teardown_handler(
    conn: AsyncConnection, job: Job, provisioning: LocalLibvirtProvisioning
) -> str | None:
    """Destroy+undefine the domain and drive the System ``-> torn_down`` (idempotent)."""
    system_id = UUID(job.payload["system_id"])
    domain_name: str | None = None
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.state is SystemState.TORN_DOWN:
            return str(system_id) if system is not None else None
        domain_name = system.domain_name or domain_name_for(system_id)
        old = system.state
        await SYSTEMS.update_state(conn, system_id, SystemState.TORN_DOWN)
        await _audit_transition(
            conn, job, project=system.project, object_id=system_id,
            transition=f"{old.value}->torn_down", tool="systems.teardown",
        )
    provisioning.teardown(domain_name)  # outside the lock (slow libvirt call)
    return str(system_id)
```

- [ ] **Step 4: Run to verify the teardown tests pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k teardown -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): teardown tool + handler (destroy/undefine, ->torn_down) (#16)"
```

---

## Task 8: Provision/teardown race — the mid-flight superseded path

**Files:**
- Modify: `tests/mcp/test_systems_tools.py`

This task adds the one test the spec's threat model headlines: a provision whose System is driven `torn_down` by a concurrent teardown *after* the provider created the domain but *before* the locked finalize. The handler must not set `ready`, must tear down the domain it created, and must not raise.

- [ ] **Step 1: Write the failing interleave test**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_provision_handler_superseded_midflight_tears_down_created_domain(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            job = await _enqueue_provision(pool, sys_id, alloc_id)

            # A provider whose provision() drives the System torn_down as a side effect,
            # simulating a concurrent teardown landing between provider.provision and finalize.
            class _RacingProvisioning(_FakeProvisioning):
                def provision(self, system_id_inner: UUID, profile: Any) -> str:
                    name = super().provision(system_id_inner, profile)

                    async def _terminate() -> None:
                        async with pool.connection() as c:
                            await SYSTEMS.update_state(c, system_id_inner, SystemState.TORN_DOWN)

                    asyncio.run_coroutine_threadsafe  # noqa: B018 - import guard placeholder
                    fut = asyncio.ensure_future(_terminate())
                    return name  # the terminate runs before the locked finalize awaits

            prov = _RacingProvisioning()
            async with pool.connection() as conn:
                result = await systems_tools.provision_handler(conn, job, prov)
            assert result == sys_id
            assert prov.torn_down == [f"kdive-{sys_id}"]  # the created domain was cleaned up
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                row = await cur.fetchone()
        assert row is not None and row["state"] == "torn_down"  # never resurrected to ready

    asyncio.run(_run())
```

> If the `ensure_future` interleave proves flaky (timing-dependent), replace the racing provider with a deterministic variant: a `provision` that itself performs the `SYSTEMS.update_state(..., TORN_DOWN)` synchronously on a fresh connection before returning the name. The assertion (no `ready`, domain cleaned up) is the invariant; the mechanism only needs to make the System terminal before `provision_handler`'s locked re-read.

- [ ] **Step 2: Run to verify it fails or is flaky, then make it deterministic**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k superseded_midflight -q`
Expected: FAIL or flaky. Rewrite the racing provider's `provision` to terminate synchronously:

```python
            class _RacingProvisioning(_FakeProvisioning):
                def provision(self, system_id_inner: UUID, profile: Any) -> str:
                    name = super().provision(system_id_inner, profile)

                    async def _terminate() -> None:
                        async with pool.connection() as c:
                            await SYSTEMS.update_state(c, system_id_inner, SystemState.TORN_DOWN)

                    asyncio.get_event_loop().run_until_complete  # not usable inside a running loop
                    return name
```

Because `provision` is a sync method called from inside the running handler coroutine, drive the terminal state from the **test** instead: seed the System `provisioning`, then have `provision` schedule nothing — directly set it terminal via a synchronous psycopg connection:

```python
            class _RacingProvisioning(_FakeProvisioning):
                def __init__(self, url: str) -> None:
                    super().__init__()
                    self._url = url

                def provision(self, system_id_inner: UUID, profile: Any) -> str:
                    name = super().provision(system_id_inner, profile)
                    import psycopg
                    with psycopg.connect(self._url, autocommit=True) as c:
                        c.execute(
                            "UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id_inner,)
                        )
                    return name

            prov = _RacingProvisioning(migrated_url)
```

This makes the System `torn_down` synchronously inside `provision()`, before `provision_handler` re-reads under the lock — deterministic.

- [ ] **Step 3: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k superseded_midflight -q`
Expected: PASS — the handler finds `torn_down` under the lock, tears down the created domain, returns without error, leaves the System `torn_down`.

- [ ] **Step 4: Run the full systems suite**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -q`
Expected: PASS (all tasks 4–8).

- [ ] **Step 5: Commit**

```bash
git add tests/mcp/test_systems_tools.py
git commit -m "test(systems): provision finalize cleans up domain when superseded mid-flight (#16)"
```

---

## Task 9: Register tools + handlers; wire into the app

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py`
- Modify: `src/kdive/mcp/app.py:21-33`
- Modify: `tests/mcp/test_systems_tools.py`
- Check: `tests/mcp/test_app.py` (the tool-surface assertion pattern)

- [ ] **Step 1: Write the failing registration tests**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_register_handlers_binds_provision_and_teardown() -> None:
    from kdive.jobs.models import HandlerRegistry

    registry = HandlerRegistry()
    systems_tools.register_handlers(registry, provisioning=_FakeProvisioning())
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None


def test_build_app_exposes_systems_tools() -> None:
    async def _run() -> None:
        from kdive.mcp.app import build_app
        from tests.mcp.conftest import make_keypair
        from fastmcp.server.auth.providers.jwt import JWTVerifier
        from tests.mcp.conftest import AUDIENCE, ISSUER

        kp = make_keypair()
        verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
        # build_app needs a pool but does not use it until a tool runs; a closed pool is fine.
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        app = build_app(pool, verifier=verifier)
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert {"systems.provision", "systems.get", "systems.teardown"} <= names

    asyncio.run(_run())
```

> Check `tests/mcp/test_app.py` first to copy its exact `build_app` + `list_tools` idiom (verifier construction, whether it uses a real or closed pool). Match that pattern rather than the sketch above if it differs.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k "register or build_app" -q`
Expected: FAIL — `register_handlers` not defined / `systems.*` not in the surface.

- [ ] **Step 3: Implement `register` + `register_handlers`**

Append to `src/kdive/mcp/tools/systems.py`:

```python
def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="systems.provision")
    async def systems_provision(allocation_id: str, profile: dict[str, Any]) -> ToolResponse:
        return await provision_system(
            pool, current_context(), allocation_id=allocation_id, profile=profile
        )

    @app.tool(name="systems.get")
    async def systems_get(system_id: str) -> ToolResponse:
        return await get_system(pool, current_context(), system_id)

    @app.tool(name="systems.teardown")
    async def systems_teardown(system_id: str) -> ToolResponse:
        return await teardown_system(pool, current_context(), system_id)


def register_handlers(
    registry: HandlerRegistry, *, provisioning: LocalLibvirtProvisioning | None = None
) -> None:
    """Bind the `provision`/`teardown` job handlers; build the provider lazily from env.

    Building the provider does not open a libvirt connection (the ``connect`` lambda is lazy),
    so the worker boots without a reachable host; the first job is the first connection.
    """
    prov = provisioning or LocalLibvirtProvisioning.from_env()

    async def _provision(conn: AsyncConnection, job: Job) -> str | None:
        return await provision_handler(conn, job, prov)

    async def _teardown(conn: AsyncConnection, job: Job) -> str | None:
        return await teardown_handler(conn, job, prov)

    registry.register(JobKind.PROVISION, _provision)
    registry.register(JobKind.TEARDOWN, _teardown)
```

- [ ] **Step 4: Wire into `app.py`**

In `src/kdive/mcp/app.py`, add the import and append to both registrar tuples:

```python
from kdive.mcp.tools import allocations, jobs, resources, systems
```

```python
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
    resources.register,
    allocations.register,
    systems.register,
)

_HANDLER_REGISTRARS: tuple[Callable[[HandlerRegistry], None], ...] = (systems.register_handlers,)
```

- [ ] **Step 5: Run to verify registration + app tests pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py tests/mcp/test_app.py -q`
Expected: PASS.

> Note: `build_handler_registry()` now calls `systems.register_handlers(registry)` with **no** injected provider, so it runs `LocalLibvirtProvisioning.from_env()`. Confirm `from_env` does not connect (Task 3 test). If `tests/mcp/test_main.py` exercises `build_handler_registry`, run it too and confirm it still passes without a libvirt host.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/systems.py src/kdive/mcp/app.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): register systems.* tools and job handlers (#16)"
```

---

## Task 10: Full guardrails + reconciler regression

**Files:** none (verification only)

- [ ] **Step 1: Run the reconciler suite (the merged invariant must stay green)**

Run: `uv run python -m pytest tests/reconciler/ -q`
Expected: PASS — especially `test_mid_provision_domain_not_reaped`, `test_orphaned_system_enqueues_gc_teardown`, `test_torn_down_row_with_inflight_teardown_not_reaped`. #16 added the `teardown` *handler*; it did not touch the reconciler.

- [ ] **Step 2: Lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean (fix any finding; re-run).

- [ ] **Step 3: Type-check (hard gate; checks src + tests)**

Run: `uv run ty check src tests`
Expected: clean. Likely touch-ups: the `libvirt.open` seam carries `# ty: ignore[invalid-argument-type]` (Task 3); `job.authorizing` reads are `Any` (fine); if `_ctx_from_job`'s `auth.get("agent_session")` trips `invalid-argument-type`, annotate the local `agent_session: str | None = auth.get("agent_session")` (do not ignore unless unavoidable).

- [ ] **Step 4: Full suite**

Run: `uv run python -m pytest -q`
Expected: PASS, no warnings. The env-gated `live_vm` tests stay deselected (none added).

- [ ] **Step 5: Commit any guardrail fixes**

```bash
git add -A
git commit -m "chore(systems): satisfy lint/type guardrails for provisioning plane (#16)"
```

---

## Self-review (spec coverage)

- Provider `render_domain_xml`/`validate_profile`/`provision`/`teardown`/`from_env` → Tasks 2–3. ✓
- `systems.provision` (mint, active flip, enqueue, idempotency, spent-allocation, param validation, authz) → Task 5. ✓
- `provision` handler (ready/failed, retry no-op, missing row, superseded) → Tasks 6, 8. ✓
- `systems.get` (own/failed/cross-project/malformed) → Task 4. ✓
- `systems.teardown` tool + handler (enqueue, already-torn-down, provisioning→torn_down, idempotent) → Task 7. ✓
- `provisioning → torn_down` state edge → Task 1. ✓
- Audit attribution from job authorizing tuple → Task 6 (`_ctx_from_job`/`_audit_transition`). ✓
- Provision/teardown SYSTEM-lock serialization (no leaked domain) → Tasks 6–8. ✓
- App + handler registration → Task 9. ✓
- Reconciler regression + guardrails → Task 10. ✓
- Non-goals (no reaper wiring, no live_vm test, no kernel/cmdline, no OCI resolution) → not implemented, by design. ✓

No spec section is unmapped.
