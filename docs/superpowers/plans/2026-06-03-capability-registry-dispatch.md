# Capability Registry, Dispatch & Plane Interfaces (M0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 provider seam — eight typed plane `Protocol`s, the `Capability`/`OpContract` value types, and an in-memory `CapabilityRegistry` that dispatches a requested operation to a provider by capability match (never by name), with deterministic multi-match resolution and typed failures — tested against fake providers.

**Architecture:** Two pure-Python modules under `src/kdive/providers/`. `capability.py` holds the `Plane`/`CleanupGuarantee` enums, the frozen-dataclass value types (`OpContract`, `Capability`, `BoundOp`), and `CapabilityRegistry` (`register` validates atomically and stores candidates keyed `(plane, operation, resource_kind)`; `dispatch` selects by pin → health → cost_class → provider_id and returns a `BoundOp`). `interfaces.py` holds the eight plane Protocols and the handle/value type aliases their signatures reference. No DB, no libvirt — the registry is in-memory and its tests are the fastest in the M0 suite. The local-libvirt provider that *implements* these Protocols is a later issue (#15).

**Tech Stack:** Python 3.13 · `dataclasses` (frozen, slots) · `enum.StrEnum` · `typing.Protocol` / `runtime_checkable` · stdlib `logging` (ADR-0014 `kdive.log` JSON setup) · `pytest` 9 (no Docker, no `pytest-asyncio`) · `ruff` (E,F,I,UP,B,SIM) · `ty` (strict; the pre-commit hook checks `src` **and** `tests`).

**Design doc:** [`docs/superpowers/specs/2026-06-03-capability-registry-dispatch-design.md`](../specs/2026-06-03-capability-registry-dispatch-design.md) · **ADR:** [`docs/adr/0022-capability-registry-dispatch-impl.md`](../../adr/0022-capability-registry-dispatch-impl.md) (refines [ADR-0009](../../adr/0009-capability-provider-dispatch.md))

---

## File structure

- Create `src/kdive/providers/capability.py` — `Plane`, `CleanupGuarantee`, `OpContract`, `Capability`, `BoundOp`, `CapabilityRegistry`.
- Create `src/kdive/providers/interfaces.py` — handle/value aliases (`SystemHandle`, `TransportHandle`, `KernelArtifact`, `ArtifactRef`, `BreakpointId`, `ProvisioningProfile`, `BuildProfile`, `BreakLocation`, `Registers`, `PowerAction`, `ResourceRecord`, `OwnedInfra`) and the eight Protocols (Discovery, Provisioning, Build, Install, Connect, Debug, Control, Retrieve).
- Create `tests/providers/__init__.py` (empty marker), `tests/providers/conftest.py` (fake providers + capability helpers), `tests/providers/test_capability_types.py`, `tests/providers/test_interfaces.py`, `tests/providers/test_registry.py`.

`src/kdive/providers/__init__.py` already exists (empty) and stays empty.

**Test command:** `uv run python -m pytest tests/providers -q` — no Docker, no env gating.

**ty on tests.** The pre-commit `ty` hook checks `tests/` too. Two tests intentionally pass a wrong type or mutate a frozen instance to prove a runtime guard fires; each such line carries a scoped `# ty: ignore[<code>]` with a justification. If `ty` reports a different rule code than the plan names, use the code `ty` actually prints (per the repo convention — ty's rule name, not mypy's).

**Import hygiene (ruff `F` + `I`).** Each task adds only the imports its own code uses; run `uv run ruff check --fix tests/providers src/kdive/providers` before each commit to auto-sort/merge imports.

---

## Task 1: `capability.py` value types — enums + frozen dataclasses

Pure value types, no registry yet — fastest feedback first.

**Files:**
- Create: `src/kdive/providers/capability.py`
- Create: `tests/providers/__init__.py`, `tests/providers/test_capability_types.py`

- [ ] **Step 1: Create the empty test package marker**

Create `tests/providers/__init__.py` with no content (an empty file).

- [ ] **Step 2: Write the failing tests**

Create `tests/providers/test_capability_types.py`:

```python
"""Tests for the capability value types (ADR-0022, issue #13)."""

from __future__ import annotations

import dataclasses

import pytest

from kdive.domain.models import ResourceKind
from kdive.providers.capability import (
    BoundOp,
    Capability,
    CleanupGuarantee,
    OpContract,
    Plane,
)


def _contract() -> OpContract:
    return OpContract(
        idempotent=True,
        destructive=False,
        cancelable=False,
        long_running=True,
        cleanup=CleanupGuarantee.BEST_EFFORT,
    )


def test_plane_enum_has_the_eight_provider_planes() -> None:
    assert {p.value for p in Plane} == {
        "discovery",
        "provisioning",
        "build",
        "install",
        "connect",
        "debug",
        "control",
        "retrieve",
    }
    assert len(Plane) == 8


def test_cleanup_guarantee_values() -> None:
    assert {c.value for c in CleanupGuarantee} == {
        "clean-rollback",
        "best-effort",
        "orphan-flagged",
    }


def test_opcontract_is_frozen() -> None:
    contract = _contract()
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.idempotent = False  # ty: ignore[invalid-assignment]  # prove frozen raises


def test_opcontract_and_capability_are_hashable() -> None:
    cap = Capability(
        plane=Plane.BUILD,
        operation="build",
        resource_kind=ResourceKind.LOCAL_LIBVIRT,
        contract=_contract(),
    )
    # Hashable → usable as a set member / dict key (registry key components).
    assert {cap, cap} == {cap}
    assert {_contract()} == {_contract()}


def test_malformed_cleanup_raises() -> None:
    with pytest.raises(TypeError):
        OpContract(
            idempotent=True,
            destructive=False,
            cancelable=False,
            long_running=False,
            cleanup="bogus",  # ty: ignore[invalid-argument-type]  # prove runtime guard
        )


def test_boundop_carries_contract_and_callable() -> None:
    contract = _contract()
    called: list[str] = []

    def fake_call() -> str:
        called.append("x")
        return "ok"

    bound = BoundOp(
        provider_id="p-1", operation="build", contract=contract, call=fake_call
    )
    assert bound.provider_id == "p-1"
    assert bound.contract is contract
    assert bound.call() == "ok"
    assert called == ["x"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/test_capability_types.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.providers.capability'`.

- [ ] **Step 4: Write the implementation**

Create `src/kdive/providers/capability.py`:

```python
"""Capability value types and the provider dispatch registry (ADR-0022).

The provider seam's core: providers register capabilities keyed
``(plane, operation, resource_kind)``; the registry dispatches a requested
operation to a provider by capability match, never by name (ADR-0009). The value
types here are frozen, hashable in-memory carriers — not persisted Pydantic
models — so a :class:`Capability` can be a registry key component and an
:class:`OpContract` rejects a malformed ``cleanup`` at construction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from kdive.domain.models import ResourceKind


class Plane(StrEnum):
    """The eight provider planes (ADR-0009). Allocation is core, not a plane."""

    DISCOVERY = "discovery"
    PROVISIONING = "provisioning"
    BUILD = "build"
    INSTALL = "install"
    CONNECT = "connect"
    DEBUG = "debug"
    CONTROL = "control"
    RETRIEVE = "retrieve"


class CleanupGuarantee(StrEnum):
    """An op's cancel/abandon cleanup guarantee (ADR-0009)."""

    CLEAN_ROLLBACK = "clean-rollback"
    BEST_EFFORT = "best-effort"
    ORPHAN_FLAGGED = "orphan-flagged"


@dataclass(frozen=True, slots=True)
class OpContract:
    """Contract flags an operation declares (ADR-0009).

    ``long_running`` routes the op as a job; ``destructive`` drives the
    destructive-op gate; ``cancelable``/``cleanup`` drive cancel and the
    reconciler.
    """

    idempotent: bool
    destructive: bool
    cancelable: bool
    long_running: bool
    cleanup: CleanupGuarantee

    def __post_init__(self) -> None:
        if not isinstance(self.cleanup, CleanupGuarantee):
            raise TypeError(
                f"cleanup must be a CleanupGuarantee, got {type(self.cleanup).__name__}"
            )


@dataclass(frozen=True, slots=True)
class Capability:
    """An advertised operation on a plane for a resource kind, with its contract."""

    plane: Plane
    operation: str
    resource_kind: ResourceKind
    contract: OpContract


@dataclass(frozen=True, slots=True)
class BoundOp:
    """A dispatched operation: the chosen provider's bound method plus its contract.

    Callers read :attr:`contract` for job routing, the destructive-op gate, and the
    reconciler without re-deriving it from the registry.
    """

    provider_id: str
    operation: str
    contract: OpContract
    call: Callable[..., object]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/test_capability_types.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Guardrails**

Run:
```bash
uv run ruff check --fix tests/providers src/kdive/providers
uv run ruff format src/kdive/providers/capability.py tests/providers/test_capability_types.py
uv run ruff check src/kdive/providers tests/providers
uv run ty check src tests
```
Expected: all clean (zero warnings). If `ty` names a different code on either `# ty: ignore` line, replace the code with the one `ty` prints.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/capability.py tests/providers/__init__.py tests/providers/test_capability_types.py
git commit -m "feat(providers): capability value types and plane/cleanup enums"
```

---

## Task 2: `interfaces.py` — plane Protocols, aliases, and the fake providers

**Files:**
- Create: `src/kdive/providers/interfaces.py`
- Create: `tests/providers/conftest.py`, `tests/providers/test_interfaces.py`

- [ ] **Step 1: Write the fake providers in `conftest.py`**

Create `tests/providers/conftest.py`. The fakes serve both the interface-conformance test and the registry tests in Tasks 3–4.

```python
"""Fakes and helpers for the provider-seam tests (issue #13).

``FakeProvider`` implements every plane method (satisfies all eight Protocols and
can be registered for any operation). ``PartialFakeProvider`` implements only
Build + Discovery. ``UnhonoredProvider`` has no plane methods. ``MutableProvider``
exposes ``build`` as an instance attribute so a test can delete it after
registration to exercise the at-dispatch honored-method re-check.
"""

from __future__ import annotations

from kdive.domain.models import ResourceKind
from kdive.providers.capability import Capability, CleanupGuarantee, OpContract, Plane
from kdive.providers.interfaces import (
    Allocation,
    ArtifactRef,
    BreakLocation,
    BreakpointId,
    BuildProfile,
    KernelArtifact,
    OwnedInfra,
    PowerAction,
    ProvisioningProfile,
    Registers,
    ResourceRecord,
    Run,
    SystemHandle,
    TransportHandle,
)

LIBVIRT = ResourceKind.LOCAL_LIBVIRT

DEFAULT_CONTRACT = OpContract(
    idempotent=True,
    destructive=False,
    cancelable=False,
    long_running=True,
    cleanup=CleanupGuarantee.BEST_EFFORT,
)


def build_capability(
    *,
    plane: Plane = Plane.BUILD,
    operation: str = "build",
    contract: OpContract = DEFAULT_CONTRACT,
) -> Capability:
    """Construct a Capability for the local-libvirt kind (test helper)."""
    return Capability(
        plane=plane, operation=operation, resource_kind=LIBVIRT, contract=contract
    )


class FakeProvider:
    """A provider exposing a method for every plane operation."""

    def list_resources(self) -> list[ResourceRecord]:
        return []

    def list_owned(self) -> list[OwnedInfra]:
        return []

    def provision(self, alloc: Allocation, profile: ProvisioningProfile) -> SystemHandle:
        return SystemHandle("sys-1")

    def teardown(self, system: SystemHandle) -> None:
        return None

    def build(self, run: Run, profile: BuildProfile) -> KernelArtifact:
        return KernelArtifact("kernel-1")

    def install(self, system: SystemHandle, kernel: KernelArtifact) -> None:
        return None

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        return TransportHandle("transport-1")

    def close_transport(self, handle: TransportHandle) -> None:
        return None

    def set_breakpoint(self, h: TransportHandle, loc: BreakLocation) -> BreakpointId:
        return BreakpointId("bp-1")

    def read_memory(self, h: TransportHandle, addr: int, length: int) -> bytes:
        return b""

    def read_registers(self, h: TransportHandle) -> Registers:
        return {}

    def power(self, system: SystemHandle, action: PowerAction) -> None:
        return None

    def force_crash(self, system: SystemHandle) -> None:
        return None

    def capture_vmcore(self, system: SystemHandle) -> ArtifactRef:
        return ArtifactRef("artifact-1")


class PartialFakeProvider:
    """Implements only the Build and Discovery planes."""

    def list_resources(self) -> list[ResourceRecord]:
        return []

    def list_owned(self) -> list[OwnedInfra]:
        return []

    def build(self, run: Run, profile: BuildProfile) -> KernelArtifact:
        return KernelArtifact("kernel-1")


class UnhonoredProvider:
    """Advertises capabilities it has no method for (no plane methods at all)."""


class MutableProvider:
    """Exposes ``build`` as a deletable instance attribute (at-dispatch re-check)."""

    def __init__(self) -> None:
        def build(run: Run, profile: BuildProfile) -> KernelArtifact:
            return KernelArtifact("kernel-1")

        self.build = build
```

- [ ] **Step 2: Write the failing interface tests**

Create `tests/providers/test_interfaces.py`:

```python
"""Tests for the plane Protocols (ADR-0009 / ADR-0022, issue #13)."""

from __future__ import annotations

import kdive.providers.interfaces as interfaces
from kdive.providers.capability import Plane
from kdive.providers.interfaces import (
    BuildPlane,
    ConnectPlane,
    ControlPlane,
    DebugPlane,
    DiscoveryPlane,
    InstallPlane,
    ProvisioningPlane,
    RetrievePlane,
)

from tests.providers.conftest import FakeProvider, PartialFakeProvider


def _assert_static_conformance(
    discovery: DiscoveryPlane,
    provisioning: ProvisioningPlane,
    build: BuildPlane,
    install: InstallPlane,
    connect: ConnectPlane,
    debug: DebugPlane,
    control: ControlPlane,
    retrieve: RetrievePlane,
) -> None:
    """`ty` checks each argument satisfies its Protocol at the call site below."""


def test_full_fake_satisfies_every_plane_protocol() -> None:
    provider = FakeProvider()
    # Static signature gate (checked by ty): a mismatch fails the typecheck.
    _assert_static_conformance(
        provider, provider, provider, provider, provider, provider, provider, provider
    )
    # Runtime presence smoke-test (runtime_checkable checks method names only).
    for plane in (
        DiscoveryPlane,
        ProvisioningPlane,
        BuildPlane,
        InstallPlane,
        ConnectPlane,
        DebugPlane,
        ControlPlane,
        RetrievePlane,
    ):
        assert isinstance(provider, plane)


def test_partial_fake_satisfies_only_implemented_planes() -> None:
    provider = PartialFakeProvider()
    assert isinstance(provider, BuildPlane)
    assert isinstance(provider, DiscoveryPlane)
    assert not isinstance(provider, ControlPlane)
    assert not isinstance(provider, ProvisioningPlane)


def test_eight_planes_present_and_allocation_absent() -> None:
    assert len(Plane) == 8
    assert not hasattr(interfaces, "AllocationPlane")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/test_interfaces.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.providers.interfaces'`.

- [ ] **Step 4: Write the implementation**

Create `src/kdive/providers/interfaces.py`:

```python
"""The eight provider-plane Protocols and their handle/value aliases (ADR-0009).

A provider implements only the planes it supports; the registry
(:mod:`kdive.providers.capability`) dispatches by capability match. The cross-plane
handle types are thin aliases in M0 — the concrete classes land with the
local-libvirt provider (#15). ``ProvisioningProfile``/``BuildProfile`` are M0
placeholders (the durable models hold them as inline ``jsonb`` fields, not named
types; a typed model arrives with ADR-0011 / #11). The ninth plane, Allocation, is
the core capacity-checked path — deliberately **not** a Protocol here.
"""

from __future__ import annotations

from typing import Any, NewType, Protocol, TypeAlias, TypedDict, runtime_checkable

from kdive.domain.models import Allocation, Run

SystemHandle = NewType("SystemHandle", str)
TransportHandle = NewType("TransportHandle", str)
KernelArtifact = NewType("KernelArtifact", str)
ArtifactRef = NewType("ArtifactRef", str)
BreakpointId = NewType("BreakpointId", str)

ProvisioningProfile: TypeAlias = dict[str, Any]
BuildProfile: TypeAlias = dict[str, Any]
BreakLocation: TypeAlias = dict[str, Any]
Registers: TypeAlias = dict[str, Any]
PowerAction: TypeAlias = str


class ResourceRecord(TypedDict):
    """A discovered resource host (Discovery plane)."""

    resource_id: str
    kind: str
    capabilities: dict[str, Any]
    status: str


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler (Discovery plane)."""

    system_id: str
    domain_name: str


@runtime_checkable
class DiscoveryPlane(Protocol):
    def list_resources(self) -> list[ResourceRecord]: ...
    def list_owned(self) -> list[OwnedInfra]: ...


@runtime_checkable
class ProvisioningPlane(Protocol):
    def provision(self, alloc: Allocation, profile: ProvisioningProfile) -> SystemHandle: ...
    def teardown(self, system: SystemHandle) -> None: ...


@runtime_checkable
class BuildPlane(Protocol):
    def build(self, run: Run, profile: BuildProfile) -> KernelArtifact: ...


@runtime_checkable
class InstallPlane(Protocol):
    def install(self, system: SystemHandle, kernel: KernelArtifact) -> None: ...


@runtime_checkable
class ConnectPlane(Protocol):
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...


@runtime_checkable
class DebugPlane(Protocol):
    def set_breakpoint(self, h: TransportHandle, loc: BreakLocation) -> BreakpointId: ...
    def read_memory(self, h: TransportHandle, addr: int, length: int) -> bytes:
        """Read guest memory. ``length`` must be ≤ 4096 (enforced by the provider, #15)."""
        ...

    def read_registers(self, h: TransportHandle) -> Registers: ...


@runtime_checkable
class ControlPlane(Protocol):
    def power(self, system: SystemHandle, action: PowerAction) -> None: ...
    def force_crash(self, system: SystemHandle) -> None: ...


@runtime_checkable
class RetrievePlane(Protocol):
    def capture_vmcore(self, system: SystemHandle) -> ArtifactRef: ...
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/test_interfaces.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Guardrails**

Run:
```bash
uv run ruff check --fix tests/providers src/kdive/providers
uv run ruff format src/kdive/providers/interfaces.py tests/providers/conftest.py tests/providers/test_interfaces.py
uv run ruff check src/kdive/providers tests/providers
uv run ty check src tests
```
Expected: clean. `ty` must accept `_assert_static_conformance(provider, ...)` — if it reports a Protocol mismatch, the `FakeProvider` method signature in `conftest.py` does not match the Protocol; fix the fake (not the Protocol) until it passes.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/interfaces.py tests/providers/conftest.py tests/providers/test_interfaces.py
git commit -m "feat(providers): eight plane Protocols and handle aliases"
```

---

## Task 3: `CapabilityRegistry.register` — atomic validation + candidate storage

**Files:**
- Modify: `src/kdive/providers/capability.py` (append the registry)
- Create: `tests/providers/test_registry.py`

- [ ] **Step 1: Write the failing register tests**

Create `tests/providers/test_registry.py`:

```python
"""Tests for CapabilityRegistry.register / dispatch (ADR-0022, issue #13)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.capability import (
    CapabilityRegistry,
    CleanupGuarantee,
    OpContract,
    Plane,
)
from kdive.domain.state import ResourceStatus

from tests.providers.conftest import (
    LIBVIRT,
    FakeProvider,
    UnhonoredProvider,
    build_capability,
)


def _registry_with_one_build_provider() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    return registry


def test_register_then_dispatch_returns_bound_op() -> None:
    registry = _registry_with_one_build_provider()
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-1"
    assert bound.operation == "build"
    assert bound.contract.long_running is True
    assert str(bound.call(None, {})) == "kernel-1"


def test_empty_provider_id_raises_value_error() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(ValueError, match="provider_id"):
        registry.register(
            FakeProvider(),
            [build_capability()],
            provider_id="",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_duplicate_provider_id_raises_value_error() -> None:
    registry = _registry_with_one_build_provider()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            FakeProvider(),
            [build_capability(operation="install", plane=Plane.INSTALL)],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_same_key_twice_in_one_call_raises_and_registers_nothing() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(ValueError, match="twice"):
        registry.register(
            FakeProvider(),
            [build_capability(), build_capability()],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    # Atomic: nothing registered, id still free.
    with pytest.raises(CategorizedError):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )


def test_unhonored_capability_raises_not_implemented_at_register() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(CategorizedError) as exc:
        registry.register(
            UnhonoredProvider(),
            [build_capability()],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_register_is_atomic_on_partial_failure() -> None:
    registry = CapabilityRegistry()
    # First cap honored (FakeProvider.build exists), second unhonored (no ghost_op).
    with pytest.raises(CategorizedError):
        registry.register(
            FakeProvider(),
            [build_capability(), build_capability(operation="ghost_op")],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    # The honored 'build' cap must NOT have been recorded.
    with pytest.raises(CategorizedError):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    # And the id is free for a corrected retry.
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )


def test_contract_divergence_across_providers_raises() -> None:
    registry = _registry_with_one_build_provider()
    diverging = OpContract(
        idempotent=True,
        destructive=True,  # differs from DEFAULT_CONTRACT.destructive
        cancelable=False,
        long_running=True,
        cleanup=CleanupGuarantee.BEST_EFFORT,
    )
    with pytest.raises(ValueError, match="contract"):
        registry.register(
            FakeProvider(),
            [build_capability(contract=diverging)],
            provider_id="p-2",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_equal_contract_second_provider_registers() -> None:
    registry = _registry_with_one_build_provider()
    registry.register(
        FakeProvider(),
        [build_capability()],  # equal contract
        provider_id="p-2",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    # Two candidates now under the same key; dispatch resolves deterministically.
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-1"  # provider_id tiebreak
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/test_registry.py -q`
Expected: FAIL with `ImportError: cannot import name 'CapabilityRegistry'`.

- [ ] **Step 3: Append the registry (register only) to `capability.py`**

Add these imports to the existing `from __future__`/import block at the top of `src/kdive/providers/capability.py` (merge into the existing lines; keep them sorted):

```python
import logging
from collections.abc import Sequence

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import ResourceStatus
```

Append to the end of `src/kdive/providers/capability.py`:

```python
_log = logging.getLogger(__name__)

_HEALTH_RANK: dict[ResourceStatus, int] = {
    ResourceStatus.AVAILABLE: 0,
    ResourceStatus.DEGRADED: 1,
    ResourceStatus.OFFLINE: 2,
}

_Key = tuple[Plane, str, ResourceKind]


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A registered provider plus the metadata dispatch orders it by."""

    provider: object
    provider_id: str
    health: ResourceStatus
    cost_class: str
    capability: Capability


def _key(capability: Capability) -> _Key:
    return (capability.plane, capability.operation, capability.resource_kind)


class CapabilityRegistry:
    """In-memory registry: register providers, dispatch by capability match.

    Built once at startup and immutable thereafter (ADR-0022) — there is no
    update/replace path. Dispatch never re-queries health; the registration-time
    snapshot is authoritative for the registry's lifetime.
    """

    def __init__(self) -> None:
        self._candidates: dict[_Key, list[_Candidate]] = {}
        self._provider_ids: set[str] = set()

    def register(
        self,
        provider: object,
        capabilities: Sequence[Capability],
        *,
        provider_id: str,
        health: ResourceStatus,
        cost_class: str,
    ) -> None:
        """Register a provider's advertised capabilities atomically.

        Validates everything before mutating registry state; on any failure the
        registry is unchanged and ``provider_id`` stays free.

        Args:
            provider: The provider object; must expose a callable named for each
                capability's ``operation``.
            capabilities: The capabilities this provider advertises.
            provider_id: Stable, non-empty, registry-unique id (the dispatch
                tiebreak).
            health: Registration-time health snapshot.
            cost_class: The provider's cost class (dispatch orders ascending).

        Raises:
            ValueError: Empty/duplicate ``provider_id``, a key advertised twice in
                this call, or a contract that diverges from an existing provider's
                contract for the same key.
            CategorizedError: ``NOT_IMPLEMENTED`` if a capability's ``operation`` is
                not a callable on ``provider``.
        """
        if not provider_id:
            raise ValueError("provider_id must be non-empty")
        if provider_id in self._provider_ids:
            raise ValueError(f"provider_id {provider_id!r} already registered")

        seen: set[_Key] = set()
        for capability in capabilities:
            key = _key(capability)
            if key in seen:
                raise ValueError(
                    f"provider {provider_id!r} advertises {key} twice in one call"
                )
            seen.add(key)
            method = getattr(provider, capability.operation, None)
            if not callable(method):
                raise CategorizedError(
                    f"provider {provider_id!r} advertises {capability.operation!r} "
                    "but has no such method",
                    category=ErrorCategory.NOT_IMPLEMENTED,
                    details={"operation": capability.operation, "provider_id": provider_id},
                )
            existing = self._candidates.get(key)
            if existing and existing[0].capability.contract != capability.contract:
                raise ValueError(
                    f"contract for {key} diverges from an already-registered provider"
                )

        self._provider_ids.add(provider_id)
        for capability in capabilities:
            self._candidates.setdefault(_key(capability), []).append(
                _Candidate(provider, provider_id, health, cost_class, capability)
            )
```

- [ ] **Step 4: Run the register tests**

Run: `uv run python -m pytest tests/providers/test_registry.py -q -k "register or divergence or contract or unhonored or atomic or provider_id or twice"`
Expected: the register-focused tests PASS. `test_register_then_dispatch_returns_bound_op`, `test_same_key_twice...`, `test_register_is_atomic...`, and `test_equal_contract...` reference `dispatch`, which does not exist yet — they FAIL with `AttributeError: 'CapabilityRegistry' object has no attribute 'dispatch'`. That is expected; Task 4 adds `dispatch`.

- [ ] **Step 5: Guardrails (partial)**

Run:
```bash
uv run ruff check --fix tests/providers src/kdive/providers
uv run ruff format src/kdive/providers/capability.py tests/providers/test_registry.py
uv run ruff check src/kdive/providers tests/providers
uv run ty check src tests
```
Expected: clean. (Tests that need `dispatch` still fail at runtime, but the files lint/type-check.)

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/capability.py tests/providers/test_registry.py
git commit -m "feat(providers): atomic capability registration with validation"
```

---

## Task 4: `CapabilityRegistry.dispatch` — deterministic selection + typed failures

**Files:**
- Modify: `src/kdive/providers/capability.py` (add `dispatch` + a selection helper)
- Modify: `tests/providers/test_registry.py` (add dispatch/ordering tests)

- [ ] **Step 1: Add the failing dispatch tests**

Append to `tests/providers/test_registry.py`:

```python
def _register(
    registry: CapabilityRegistry,
    provider_id: str,
    *,
    health: ResourceStatus = ResourceStatus.AVAILABLE,
    cost_class: str = "standard",
) -> None:
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id=provider_id,
        health=health,
        cost_class=cost_class,
    )


def test_dispatch_unregistered_op_raises_not_implemented_with_key() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.CONTROL, "force_crash", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
    assert exc.value.details["operation"] == "force_crash"
    assert exc.value.details["plane"] == Plane.CONTROL
    assert exc.value.details["resource_kind"] == LIBVIRT


def test_pin_wins_over_a_healthier_rival() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.AVAILABLE)
    _register(registry, "p-b", health=ResourceStatus.DEGRADED)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT, pin="p-b")
    assert bound.provider_id == "p-b"


def test_pin_to_non_advertising_provider_raises_not_implemented() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a")
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.BUILD, "build", LIBVIRT, pin="ghost")
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
    assert exc.value.details["pin"] == "ghost"


def test_health_beats_cost_class() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.DEGRADED, cost_class="aaa")
    _register(registry, "p-b", health=ResourceStatus.AVAILABLE, cost_class="zzz")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"  # healthier wins despite worse cost_class


def test_cost_class_beats_provider_id() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", cost_class="zzz")
    _register(registry, "p-b", cost_class="aaa")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"  # cheaper cost_class wins despite later id


def test_provider_id_is_the_final_tiebreak() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-b")
    _register(registry, "p-a")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-a"  # equal health+cost → lowest id


def test_health_never_filters_offline_only_candidate() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.OFFLINE)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-a"  # offline still dispatches


def test_degraded_beats_offline() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.OFFLINE)
    _register(registry, "p-b", health=ResourceStatus.DEGRADED)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"


def test_partial_provider_dispatches_advertised_only() -> None:
    from tests.providers.conftest import PartialFakeProvider

    registry = CapabilityRegistry()
    registry.register(
        PartialFakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    assert registry.dispatch(Plane.BUILD, "build", LIBVIRT).provider_id == "p-1"
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.CONTROL, "force_crash", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_unhonored_at_dispatch_raises_not_implemented() -> None:
    from tests.providers.conftest import MutableProvider

    registry = CapabilityRegistry()
    provider = MutableProvider()
    registry.register(
        provider,
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    del provider.build  # drop the method after registration (add a scoped
    # `# ty: ignore[...]` here only if ty objects to deleting the instance attribute)
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_dispatch_logs_the_selection(caplog: pytest.LogCaptureFixture) -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a")
    _register(registry, "p-b")
    with caplog.at_level("DEBUG", logger="kdive.providers.capability"):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert any("p-a" in record.getMessage() for record in caplog.records)
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run python -m pytest tests/providers/test_registry.py -q`
Expected: the dispatch tests FAIL with `AttributeError: 'CapabilityRegistry' object has no attribute 'dispatch'`.

- [ ] **Step 3: Add `dispatch` and the selection helper**

Append these two methods inside the `CapabilityRegistry` class in `src/kdive/providers/capability.py` (after `register`):

```python
    def dispatch(
        self,
        plane: Plane,
        operation: str,
        resource_kind: ResourceKind,
        *,
        pin: str | None = None,
    ) -> BoundOp:
        """Resolve a requested operation to a bound provider op (ADR-0009).

        Selection: an explicit ``pin`` (a ``provider_id``) wins outright; otherwise
        candidates are ordered by health, then ``cost_class`` ascending, then
        ``provider_id`` ascending, and the first is bound. Health orders but never
        filters — an ``offline``-only key still dispatches.

        Args:
            plane: The requested plane.
            operation: The requested operation (a plane method name).
            resource_kind: The resource kind to dispatch for.
            pin: Optional ``provider_id`` to force a specific provider.

        Returns:
            A :class:`BoundOp` carrying the chosen provider's bound method and the
            operation's contract.

        Raises:
            CategorizedError: ``NOT_IMPLEMENTED`` if no provider advertises the key,
                if ``pin`` names a provider that does not advertise it, or if the
                selected provider no longer exposes the method.
        """
        key = (plane, operation, resource_kind)
        details: dict[str, object] = {
            "plane": plane,
            "operation": operation,
            "resource_kind": resource_kind,
            "pin": pin,
        }
        candidates = self._candidates.get(key)
        if not candidates:
            raise CategorizedError(
                f"no provider advertises {key}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        chosen, deciding = self._select(candidates, pin)
        if chosen is None:
            raise CategorizedError(
                f"pin {pin!r} does not advertise {key}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        method = getattr(chosen.provider, operation, None)
        if not callable(method):
            raise CategorizedError(
                f"provider {chosen.provider_id!r} no longer honors {operation!r}",
                category=ErrorCategory.NOT_IMPLEMENTED,
                details=details,
            )

        _log.debug(
            "capability dispatch %s/%s/%s -> %s of %s (by %s)",
            plane,
            operation,
            resource_kind,
            chosen.provider_id,
            [c.provider_id for c in candidates],
            deciding,
        )
        return BoundOp(chosen.provider_id, operation, chosen.capability.contract, method)

    @staticmethod
    def _select(
        candidates: list[_Candidate], pin: str | None
    ) -> tuple[_Candidate | None, str]:
        """Pick the winning candidate and the step that decided it."""
        if pin is not None:
            for candidate in candidates:
                if candidate.provider_id == pin:
                    return candidate, "pin"
            return None, "pin"
        ordered = sorted(
            candidates,
            key=lambda c: (_HEALTH_RANK[c.health], c.cost_class, c.provider_id),
        )
        winner = ordered[0]
        if len(ordered) == 1:
            return winner, "sole"
        runner_up = ordered[1]
        if winner.health != runner_up.health:
            return winner, "health"
        if winner.cost_class != runner_up.cost_class:
            return winner, "cost_class"
        return winner, "provider_id"
```

- [ ] **Step 4: Run the full provider test suite**

Run: `uv run python -m pytest tests/providers -q`
Expected: PASS (all tests across the three test files — value types, interfaces, registry).

- [ ] **Step 5: Guardrails**

Run:
```bash
uv run ruff check --fix tests/providers src/kdive/providers
uv run ruff format src/kdive/providers tests/providers
uv run ruff check src/kdive/providers tests/providers
uv run ty check src tests
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/capability.py tests/providers/test_registry.py
git commit -m "feat(providers): capability dispatch with deterministic selection"
```

---

## Task 5: Full-suite verification

No new code — confirm the change is green across the whole repo and the guardrails are clean.

- [ ] **Step 1: Run the full test suite**

Run: `uv run python -m pytest -q`
Expected: PASS. The env-gated libvirt/gdb/drgn integration tests skip (expected); no provider-seam test is gated.

- [ ] **Step 2: Run all guardrails repo-wide**

Run:
```bash
uv run ruff check
uv run ruff format --check
uv run ty check src tests
```
Expected: all clean, zero warnings.

- [ ] **Step 3: Confirm acceptance criteria (issue #13)**

Verify by re-reading the tests, each maps to an acceptance criterion:
- *dispatch selects a registered fake by capability* → `test_register_then_dispatch_returns_bound_op`, `test_partial_provider_dispatches_advertised_only`.
- *an unregistered op raises `not_implemented`* → `test_dispatch_unregistered_op_raises_not_implemented_with_key`.
- *two providers matching are resolved deterministically by the documented order* → `test_pin_wins_over_a_healthier_rival`, `test_health_beats_cost_class`, `test_cost_class_beats_provider_id`, `test_provider_id_is_the_final_tiebreak`.
- *advertised-but-unhonored → typed `not_implemented`* → `test_unhonored_capability_raises_not_implemented_at_register`, `test_unhonored_at_dispatch_raises_not_implemented`.

- [ ] **Step 4: No commit** (verification only). Proceed to the branch's adversarial-review loop (`/challenge main..HEAD`).
```
