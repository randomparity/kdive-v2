# Per-kind ProviderRuntime registry (#180) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single global `build_default_provider_runtime()` with a static `ResourceKind → ProviderRuntime` registry (`ProviderResolver`); worker job handlers resolve their runtime per-op from the job's System kind, the MCP boundary and discovery are threaded through the resolver, and an unknown kind fails closed — all behavior-preserving for the only registered kind, `local-libvirt`.

**Architecture:** A new `ProviderResolver` holds a `Mapping[ResourceKind, ProviderRuntime]` built per deployment in `providers/composition.py`. Worker handlers keep their `(conn, job, port)` signature but gain a keyword `resolver=`; production passes `resolver=` and the handler resolves the port **lazily, after its existence check** (so gone-target idempotency and error categories are preserved) via `job → system → allocation → resource.kind`. MCP tool registrar **modules are unchanged** — `app.py` resolves the sole `local-libvirt` runtime from the resolver and feeds it to them as today (per-target MCP resolution is deferred to issues 2/4, when a second kind's facets become observable). Discovery fans out over the resolver's composed runtimes. A new `ResourceKind.FAULT_INJECT` enum member and a fail-closed opt-in gate are added now; the migration that widens the DB CHECK and the fault-inject runtime itself land in issue 2 — so a CHECK↔registry parity test holds at this merge with only `local-libvirt`.

**Tech Stack:** Python 3.13, `psycopg`/`psycopg_pool`, `pytest` (+ Docker-gated testcontainers Postgres), `ruff`, `ty`. Run checks via `just lint`, `just type`, `just test`.

**Decisions referenced:** [ADR-0071](../../adr/0071-per-kind-provider-runtime-registry.md), [spec §Provider model / §Decomposition issue 1](../../specs/m1.5-fault-injection-provider.md). These are merged and settled; this plan does **not** reopen them.

**Two design choices confirmed with the maintainer (do not re-litigate):**
1. *Worker-deep, MCP-threaded.* Worker handlers resolve per-op (the real selection seam issues 5/6/7 exercise). MCP tool registrar modules keep their `provider_runtime` param, fed the resolved `local-libvirt` runtime by `app.py`; per-target MCP resolution (debug/introspect/connect) lands in issues 2/4.
2. *Ship the opt-in gate now, fail closed.* `build_provider_resolver(*, enable_fault_inject=False)` exists now; default composition is `{local-libvirt}` only. Enabling it in this PR raises `configuration_error` ("fault-inject not yet registered"); issue 2 replaces the closed branch with the real registration.

**Sequencing invariant (load-bearing — `just lint`, `just type`, `just test` must be green at *every* commit):** `build_default_provider_runtime` is imported at module load by `mcp/app.py`, `__main__.py`, and `admin/bootstrap.py`; and each plane's `register_handlers` is called from `app.py`. So a rename or a signature change is only green if its callers change in the **same commit**. The tasks below are therefore grouped into two atomic wiring commits (Task 3 = introduce resolver + reroute MCP/discovery while planes still take `provider_runtime`; Task 4 = migrate the whole worker-handler path at once). Do not split Task 3 or Task 4 into per-file commits.

---

## File Structure

**Create:**
- `src/kdive/providers/resolver.py` — `ProviderResolver`: the kind→runtime map, fail-closed `resolve()`, `registered_kinds()`, `runtimes()`, `register_all_discovery()`, and async `runtime_for_system()` / `runtime_for_run()` (the `system|run → allocation → resource.kind` joins).
- `tests/providers/test_resolver.py` — unit tests for `resolve()` (hit + fail-closed), `registered_kinds()`, empty-map rejection, `register_all_discovery()` fan-out.
- `tests/db/test_resource_kind_parity.py` — Docker-gated CHECK↔registry parity test (introspects `resources_kind_check` from a migrated DB).

**Modify:**
- `src/kdive/domain/models.py` — add `ResourceKind.FAULT_INJECT = "fault-inject"`.
- `src/kdive/providers/composition.py` — rename `build_default_provider_runtime` → `build_local_runtime`; add `build_provider_resolver(*, enable_fault_inject=False)`; update `__all__`.
- `src/kdive/mcp/app.py` — thread `ProviderResolver` through both registrar seams; resolve `local-libvirt` for MCP tool registrars; (Task 4) pass the resolver to worker handler registrars; rename the `provider_runtime=` kwarg to `provider_resolver=`.
- `src/kdive/__main__.py` — `_run_reconciler`/`_register_provider_resources` use `build_provider_resolver().register_all_discovery(pool)`.
- `src/kdive/admin/bootstrap.py` — `register_local_resource` uses `build_provider_resolver().register_all_discovery(pool)`.
- `src/kdive/planes/systems.py` / `runs.py` / `control.py` / `vmcore.py` (Task 4) — handlers gain `*, resolver=None`, lazy resolve after existence check; `register_handlers(*, <port>=None, resolver=None)`; drop the now-unused `ProviderRuntime` import from `systems.py`/`control.py`/`vmcore.py` (`runs.py` keeps it — used as a return type).
- `tests/providers/test_composition.py`, `tests/providers/test_capture_capabilities.py` — `build_default_provider_runtime` → `build_local_runtime`; add default-resolver + gate assertions.
- `tests/reconciler/test_main.py` — monkeypatch `build_provider_resolver` returning a fake resolver whose `register_all_discovery` records the `discover` event.

**Untouched on purpose:** every `providers/local_libvirt/*` file (no behavioral diff); the MCP tool registrar modules (`mcp/tools/lifecycle/systems/registrar.py`, `runs/registrar.py`, `lifecycle/vmcore.py`, `debug/sessions.py`, `debug/introspect.py`) and their tests (they still take `provider_runtime`); every handler-invocation test that calls `*_handler(conn, job, port)` positionally; `tests/db/test_migrate.py::test_rerun_is_a_noop` (no new migration — DDL is issue 2).

---

## Task 1: Add the `FAULT_INJECT` resource kind

**Files:**
- Modify: `src/kdive/domain/models.py:53-56`
- Test: `tests/domain/test_models.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_models.py
from kdive.domain.models import ResourceKind


def test_resource_kind_has_local_libvirt_and_fault_inject() -> None:
    assert ResourceKind.LOCAL_LIBVIRT.value == "local-libvirt"
    assert ResourceKind.FAULT_INJECT.value == "fault-inject"
    assert {k.value for k in ResourceKind} == {"local-libvirt", "fault-inject"}
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python -m pytest tests/domain/test_models.py::test_resource_kind_has_local_libvirt_and_fault_inject -q`
Expected: FAIL — `AttributeError: FAULT_INJECT`.

- [ ] **Step 3: Add the enum member**

```python
class ResourceKind(StrEnum):
    """The provider resource kinds; M1.5 adds the fault-injection mock kind.

    ``FAULT_INJECT`` is a forward declaration: its runtime and the
    ``resources_kind_check`` widen that admits it land with the mock provider
    (M1.5 issue 2). The default production composition does not register it.
    """

    LOCAL_LIBVIRT = "local-libvirt"
    FAULT_INJECT = "fault-inject"
```

- [ ] **Step 4: Run it, expect pass.**

Run: `uv run python -m pytest tests/domain/test_models.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/models.py tests/domain/test_models.py
git commit -m "feat(providers): add fault-inject resource kind"
```

---

## Task 2: `ProviderResolver` — the kind→runtime registry

Additive — no existing caller references this module yet, so the commit is green on its own.

**Files:**
- Create: `src/kdive/providers/resolver.py`
- Test: `tests/providers/test_resolver.py`

The join queries live here (a provider-selection concern). `runtime_for_system`/`runtime_for_run` raise `configuration_error` when no kind row resolves — but handlers only call them **after** confirming the target exists, so in practice the join always finds a granted allocation's `resource_id` (non-null post-grant, ADR-0069).

- [ ] **Step 1: Write the failing unit tests** (no DB — exercise `resolve` and fan-out with fakes)

```python
# tests/providers/test_resolver.py
"""Unit tests for the per-kind ProviderResolver (ADR-0071)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.resolver import ProviderResolver


class _Runtime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.registered: list[object] = []

    async def register_discovery(self, pool: object) -> None:
        self.registered.append(pool)


def _resolver(*kinds: ResourceKind) -> tuple[ProviderResolver, dict[ResourceKind, _Runtime]]:
    runtimes = {k: _Runtime(k.value) for k in kinds}
    return ProviderResolver(cast(dict, runtimes)), runtimes


def test_resolve_returns_the_registered_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT) is runtimes[ResourceKind.LOCAL_LIBVIRT]


def test_resolve_unknown_kind_fails_closed_with_configuration_error() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.FAULT_INJECT)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "fault-inject" in str(exc.value)


def test_registered_kinds_reflects_the_map() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})


def test_empty_resolver_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ProviderResolver({})


def test_register_all_discovery_fans_out_over_every_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    pool = cast(AsyncConnectionPool, object())
    asyncio.run(resolver.register_all_discovery(pool))
    assert runtimes[ResourceKind.LOCAL_LIBVIRT].registered == [pool]
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run python -m pytest tests/providers/test_resolver.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.resolver`.

- [ ] **Step 3: Implement `ProviderResolver`**

```python
# src/kdive/providers/resolver.py
"""Per-kind provider runtime registry (ADR-0071).

The resolver maps a ``ResourceKind`` to the ``ProviderRuntime`` that serves it.
Post-System worker ops resolve their runtime from the System's Resource kind
(``job -> system -> allocation -> resource.kind``); an unregistered kind fails
closed with ``configuration_error`` rather than falling through to a default.
Concrete runtimes are still constructed only in :mod:`kdive.providers.composition`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.runtime import ProviderRuntime

_KIND_FOR_SYSTEM: Final = (
    "SELECT r.kind AS kind FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE s.id = %s"
)
_KIND_FOR_RUN: Final = (
    "SELECT r.kind AS kind FROM runs rn "
    "JOIN systems s ON s.id = rn.system_id "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE rn.id = %s"
)


class ProviderResolver:
    """A static ``ResourceKind -> ProviderRuntime`` registry.

    Built per deployment by :func:`kdive.providers.composition.build_provider_resolver`.
    Selection is exhaustive and fail-closed: an unregistered kind raises
    ``configuration_error`` at resolution.
    """

    def __init__(self, runtimes: Mapping[ResourceKind, ProviderRuntime]) -> None:
        if not runtimes:
            raise ValueError("ProviderResolver requires at least one registered runtime")
        self._runtimes: dict[ResourceKind, ProviderRuntime] = dict(runtimes)

    def resolve(self, kind: ResourceKind) -> ProviderRuntime:
        """Return the runtime registered for ``kind`` or fail closed."""
        runtime = self._runtimes.get(kind)
        if runtime is None:
            raise CategorizedError(
                f"no provider runtime is registered for resource kind {kind.value!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "kind": kind.value,
                    "registered": sorted(k.value for k in self._runtimes),
                },
            )
        return runtime

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return frozenset(self._runtimes)

    def runtimes(self) -> tuple[ProviderRuntime, ...]:
        return tuple(self._runtimes.values())

    async def register_all_discovery(self, pool: AsyncConnectionPool) -> None:
        """Run every composed runtime's discovery registrar (discovery keys on the
        map entry's own kind, not on a Resource that does not yet exist)."""
        for runtime in self._runtimes.values():
            await runtime.register_discovery(pool)

    async def runtime_for_system(self, conn: AsyncConnection, system_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_SYSTEM, system_id, "system"))

    async def runtime_for_run(self, conn: AsyncConnection, run_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_RUN, run_id, "run"))

    async def _kind(
        self, conn: AsyncConnection, sql: str, object_id: UUID, object_kind: str
    ) -> ResourceKind:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (object_id,))
            row = await cur.fetchone()
        if row is None:
            raise CategorizedError(
                f"cannot resolve a provider runtime: no resource kind for {object_kind}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={object_kind: str(object_id)},
            )
        return ResourceKind(row["kind"])
```

- [ ] **Step 4: Run it, expect pass.**

Run: `uv run python -m pytest tests/providers/test_resolver.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/resolver.py tests/providers/test_resolver.py
git commit -m "feat(providers): add per-kind ProviderResolver registry"
```

---

## Task 3: Introduce the resolver + reroute MCP & discovery (atomic, green)

**One commit.** Add `build_provider_resolver`/`build_local_runtime`, route the MCP tool registrars and discovery through the resolver, and update every caller of the old name — all together, so the tree never breaks. **Worker handlers are untouched here**: they still receive a `ProviderRuntime` (the resolved `local-libvirt` one), so their per-op migration (Task 4) is a clean, separable change. This commit is behavior-identical (one kind, resolved eagerly for the MCP/handler seams as today).

**Files:** `src/kdive/providers/composition.py`, `src/kdive/mcp/app.py`, `src/kdive/__main__.py`, `src/kdive/admin/bootstrap.py`, `tests/providers/test_composition.py`, `tests/providers/test_capture_capabilities.py`, `tests/reconciler/test_main.py`.

- [ ] **Step 1: Update the tests first (red), then make them green with the implementation below.**

`tests/providers/test_capture_capabilities.py` — rename import + call:

```python
from kdive.providers.composition import build_local_runtime


def test_local_libvirt_supports_three_methods_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert build_local_runtime().supported_capture_methods == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
```

`tests/providers/test_composition.py` — add `import pytest`; replace the two `composition.build_default_provider_runtime()` calls (lines 150, 164) with `composition.build_local_runtime()`; append:

```python
def test_default_resolver_registers_only_local_libvirt() -> None:
    from kdive.domain.models import ResourceKind

    resolver = composition.build_provider_resolver()
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT).component_sources.provider == "local-libvirt"


def test_enabling_fault_inject_before_it_exists_fails_closed() -> None:
    from kdive.domain.errors import CategorizedError, ErrorCategory

    with pytest.raises(CategorizedError) as exc:
        composition.build_provider_resolver(enable_fault_inject=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

`tests/reconciler/test_main.py` — replace the runtime monkeypatch (line ~46) with a fake resolver:

```python
    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover")

    monkeypatch.setattr(composition, "build_provider_resolver", lambda **kw: _FakeResolver())
```

(`__main__._run_reconciler` does `from kdive.providers.composition import build_provider_resolver` at call time, so patching the attribute on `composition` is sufficient — keep the existing `from kdive.providers import composition` reference at line 28.)

- [ ] **Step 2: `composition.py` — rename + resolver builder + gate**

Add imports at the top:

```python
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.resolver import ProviderResolver
```

Rename `build_default_provider_runtime` → `build_local_runtime` (body unchanged; docstring → "Build the typed local-libvirt provider ports without opening live provider connections."). Add:

```python
def build_provider_resolver(*, enable_fault_inject: bool = False) -> ProviderResolver:
    """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry.

    The default production composition registers only ``local-libvirt``. The
    ``fault-inject`` provider is opt-in (ADR-0071) and its runtime lands in M1.5
    issue 2; enabling the gate before then is a configuration error, never a
    silent no-op.
    """
    runtimes = {ResourceKind.LOCAL_LIBVIRT: build_local_runtime()}
    if enable_fault_inject:
        raise CategorizedError(
            "fault-inject provider is not yet registered (M1.5 issue 2); "
            "do not enable the gate before its runtime exists",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"enable_fault_inject": True},
        )
    return ProviderResolver(runtimes)
```

Update `__all__` → `["build_local_runtime", "build_provider_resolver", "ensure_local_host_registered"]`.

- [ ] **Step 3: `app.py` — thread the resolver; MCP registrars get the resolved local runtime; handlers still get it too (worker migration is Task 4)**

Replace the composition import and add the resolver/ResourceKind imports:

```python
from kdive.domain.models import ResourceKind
from kdive.providers.composition import build_provider_resolver
from kdive.providers.resolver import ProviderResolver
```

Change the registrar seam types to carry the resolver:

```python
type PlaneRegistrar = Callable[
    [FastMCP, AsyncConnectionPool, ProviderResolver, SecretRegistry], None
]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderResolver], None]
```

`_plain`'s ignored slot becomes `ProviderResolver`. The provider-aware **tool** lambdas resolve the local runtime (MCP registrar modules unchanged):

```python
    lambda app, pool, resolver, registry: systems_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    ...
    lambda app, pool, resolver, registry: runs_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda app, pool, resolver, registry: vmcore_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda app, pool, resolver, registry: debug_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT),
        secret_registry=registry,
    ),
    lambda app, pool, resolver, registry: introspect.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
```

The **handler** registrars in this task STILL pass the resolved local runtime (planes are migrated in Task 4):

```python
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    lambda registry, resolver: systems.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: runs.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: control.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: vmcore.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
)
```

`build_app` / `build_handler_registry` rename the kwarg and build the resolver:

```python
def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_resolver: ProviderResolver | None = None,
    secret_registry: SecretRegistry | None = None,
) -> FastMCP:
    ...
    resolver = provider_resolver or build_provider_resolver()
    registry = PROCESS_SECRET_REGISTRY if secret_registry is None else secret_registry
    for register in _PLANE_REGISTRARS:
        register(app, pool, resolver, registry)
    return app


def build_handler_registry(*, provider_resolver: ProviderResolver | None = None) -> HandlerRegistry:
    registry = HandlerRegistry()
    resolver = provider_resolver or build_provider_resolver()
    for register in _HANDLER_REGISTRARS:
        register(registry, resolver)
    return registry
```

Update both docstrings to say "provider resolver".

- [ ] **Step 4: `__main__.py` — reconciler discovery via the resolver**

In `_run_reconciler` replace the import and call:

```python
    from kdive.providers.composition import build_provider_resolver
    ...
    await _register_provider_resources(pool, build_provider_resolver())
```

Change `_register_provider_resources` to take a `ProviderResolver`:

```python
async def _register_provider_resources(
    pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    """Best-effort provider discovery registration so allocations.request has a Resource."""
    try:
        await resolver.register_all_discovery(pool)
    except Exception:  # noqa: BLE001 - registration failure must not crash the reconciler
        _log.warning("reconciler: provider discovery registration failed at startup", exc_info=True)
```

Update the `TYPE_CHECKING` block (line 24-25): import `ProviderResolver` instead of `ProviderRuntime`.

- [ ] **Step 5: `bootstrap.py` — `register_local_resource` via the resolver**

Replace the body of `register_local_resource` (currently at `bootstrap.py:143-146`):

```python
async def register_local_resource(pool: Any) -> None:
    from kdive.providers.composition import build_provider_resolver

    await build_provider_resolver().register_all_discovery(pool)
```

- [ ] **Step 6: Run the affected suites — must be green (this is the wiring commit; verify broadly)**

Run: `uv run python -m pytest tests/providers/test_composition.py tests/providers/test_capture_capabilities.py tests/mcp/core/test_app.py tests/reconciler/test_main.py tests/mcp/debug/test_introspect_tools.py -q`
Expected: PASS. (`test_app.py` exercises both `build_app` import and `build_handler_registry` binding — it would catch any rename/signature breakage.)

- [ ] **Step 7: lint + type, then commit**

```bash
just lint && just type
git add src/kdive/providers/composition.py src/kdive/mcp/app.py src/kdive/__main__.py \
        src/kdive/admin/bootstrap.py tests/providers/test_composition.py \
        tests/providers/test_capture_capabilities.py tests/reconciler/test_main.py
git commit -m "feat(providers): build a ProviderResolver and route MCP+discovery through it"
```

---

## Task 4: Migrate the worker-handler path to per-op resolution (atomic, green)

**One commit.** All four planes' handlers + `register_handlers`, plus `app.py`'s `_HANDLER_REGISTRARS`, change together (they are coupled through `build_handler_registry`). The handler bodies keep their positional `port` parameter (so positional-injection tests are unaffected) and gain `*, resolver=None`; the port is resolved **lazily, immediately before its first use**, after the existence/precheck guard.

> Positional handler-invocation tests (`provision_handler(conn, job, prov)`, `build_handler(conn, job, builder)`, `capture_handler(conn, job, retriever)`) bind the port positionally and leave `resolver=None` — unaffected. Production passes `resolver=`.

- [ ] **Step 1: `systems.py`** — replace the `ProviderRuntime` import with `from kdive.providers.resolver import ProviderResolver`. Add the helper and migrate the three handlers + `register_handlers`:

```python
async def _provisioner(
    conn: AsyncConnection,
    system_id: UUID,
    explicit: Provisioner | None,
    resolver: ProviderResolver | None,
) -> Provisioner:
    if explicit is not None:
        return explicit
    if resolver is None:
        raise RuntimeError("provision handlers require an explicit provisioner or a resolver")
    return (await resolver.runtime_for_system(conn, system_id)).provisioner
```

- `provision_handler(conn, job, provisioning=None, *, resolver=None)`: after `if system is None: raise ...`, insert `provisioning = await _provisioner(conn, system_id, provisioning, resolver)` (before the `system.state` branch, which already uses the port in its terminal-state teardown path).
- `reprovision_handler(conn, job, provisioning=None, *, resolver=None)`: after its `if system is None: raise ...`, insert the same resolution line.
- `teardown_handler(conn, job, provisioning=None, *, resolver=None)`: the `domain_name` is captured inside the locked `async with` block, but `provisioning.teardown(domain_name)` is called **after** that block exits (current `systems.py:281`). Resolve **after** the `async with` block, immediately before the teardown call: `provisioning = await _provisioner(conn, system_id, provisioning, resolver)`. (Skipped naturally when `system is None` returns early inside the block.)

```python
def register_handlers(
    registry: HandlerRegistry,
    *,
    provisioning: Provisioner | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `provision`/`teardown`/`reprovision` job handlers."""
    if provisioning is None and resolver is None:
        raise RuntimeError("systems handlers require a resolver or an explicit provisioner")
    registry.register(
        JobKind.PROVISION,
        lambda conn, job: provision_handler(conn, job, provisioning, resolver=resolver),
    )
    registry.register(
        JobKind.TEARDOWN,
        lambda conn, job: teardown_handler(conn, job, provisioning, resolver=resolver),
    )
    registry.register(
        JobKind.REPROVISION,
        lambda conn, job: reprovision_handler(conn, job, provisioning, resolver=resolver),
    )
```

- [ ] **Step 2: `runs.py`** — add `from kdive.providers.resolver import ProviderResolver` (keep the existing `ProviderRuntime` import — used below as a return type). Add:

```python
async def _run_runtime(
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver | None
) -> ProviderRuntime:
    if resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")
    return await resolver.runtime_for_run(conn, run_id)
```

- `build_handler(conn, job, builder=None, *, resolver=None)`: the builder is used only inside `if result is None:` (i.e. `builder.build(...)`). Resolve there, right before the call, to avoid resolving for an idempotent re-build whose result already exists:

```python
    result = await existing_build_result(conn, run_id)
    if result is None:
        if builder is None:
            builder = (await _run_runtime(conn, run_id, resolver)).builder
        try:
            output = await asyncio.to_thread(builder.build, run_id, parsed)
        ...
```

- `install_handler(conn, job, installer=None, *, resolver=None)`: after the `system` existence check and before `claim_run_step`, resolve via the already-loaded system:

```python
    if installer is None:
        installer = (await _run_runtime_for_system(conn, run.system_id, resolver)).installer
```

with a sibling helper resolving by system (or inline `runtime_for_system`); to avoid two helpers, inline it:

```python
    if installer is None:
        if resolver is None:
            raise RuntimeError("runs handlers require a resolver or explicit run ports")
        installer = (await resolver.runtime_for_system(conn, run.system_id)).installer
```

- `boot_handler(conn, job, booter=None, *, resolver=None)`: after `if run is None: raise ...` and before `claim_run_step`, resolve via run:

```python
    if booter is None:
        booter = (await _run_runtime(conn, run_id, resolver)).booter
```

`register_handlers`:

```python
def register_handlers(
    registry: HandlerRegistry,
    *,
    builder: Builder | None = None,
    installer: Installer | None = None,
    booter: Booter | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    if builder is None and installer is None and booter is None and resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")
    registry.register(
        JobKind.BUILD, lambda conn, job: build_handler(conn, job, builder, resolver=resolver)
    )
    registry.register(
        JobKind.INSTALL, lambda conn, job: install_handler(conn, job, installer, resolver=resolver)
    )
    registry.register(
        JobKind.BOOT, lambda conn, job: boot_handler(conn, job, booter, resolver=resolver)
    )
```

- [ ] **Step 3: `control.py`** — replace the `ProviderRuntime` import with `ProviderResolver`. Add:

```python
async def _controller(
    conn: AsyncConnection, system_id: UUID, resolver: ProviderResolver | None
) -> Controller:
    if resolver is None:
        raise RuntimeError("control handlers require a resolver or an explicit controller")
    return (await resolver.runtime_for_system(conn, system_id)).controller
```

- `power_handler(conn, job, control=None, *, resolver=None)`: `target = await _control_target(...)` already raises if gone; resolve after it, before `control.power(...)`: `control = control if control is not None else await _controller(conn, system_id, resolver)`.
- `force_crash_handler(conn, job, control=None, *, resolver=None)`: `target = await _force_crash_target(...)`; `if target is None: return str(system_id)` (port unused); after that, before `control.force_crash(...)`: `control = control if control is not None else await _controller(conn, system_id, resolver)`.

`register_handlers(*, control=None, resolver=None)` — same guard + pass-through pattern as Step 1, binding `POWER`/`FORCE_CRASH`.

- [ ] **Step 4: `vmcore.py`** — replace the `ProviderRuntime` import with `ProviderResolver`. `precheck_system` returns `str` (existing same-method key → early return, port unused) or the `System`. Resolve only on the `System` branch:

```python
    precheck = await precheck_system(conn, system_id, method)
    if isinstance(precheck, str):
        return precheck
    if retriever is None:
        if resolver is None:
            raise RuntimeError("vmcore handlers require a resolver or an explicit retriever")
        retriever = (await resolver.runtime_for_system(conn, system_id)).retriever
    output = await asyncio.to_thread(retriever.capture, system_id, method)
    return await finalize_capture(conn, job, precheck, method, output)
```

`register_handlers(*, retriever=None, resolver=None)` — same guard + pass-through, binding `CAPTURE_VMCORE`.

- [ ] **Step 5: `app.py`** — flip every `_HANDLER_REGISTRARS` lambda to pass the resolver:

```python
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    lambda registry, resolver: systems.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: runs.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: control.register_handlers(registry, resolver=resolver),
    lambda registry, resolver: vmcore.register_handlers(registry, resolver=resolver),
)
```

- [ ] **Step 6: Run the handler + wiring suites — must be green**

Run: `uv run python -m pytest tests/mcp/core/test_app.py tests/mcp/lifecycle/test_control_tools.py tests/mcp/lifecycle/test_vmcore_tools.py tests/adversarial/test_provider_state_races.py tests/integration/test_walking_skeleton.py -q`
Expected: PASS (Docker-gated suites skip cleanly without Docker; `test_app.py` always runs and confirms `build_handler_registry` binding).

- [ ] **Step 7: lint + type (catches any orphaned `ProviderRuntime` import), then commit**

```bash
just lint && just type
git add src/kdive/planes/systems.py src/kdive/planes/runs.py src/kdive/planes/control.py \
        src/kdive/planes/vmcore.py src/kdive/mcp/app.py
git commit -m "feat(providers): resolve worker handler provider per System/Run kind"
```

---

## Task 5: CHECK↔registry parity test

Assert every kind the live `resources_kind_check` admits has a registered, reachable runtime, and the reverse. Docker-gated (uses the `pg_conn` fixture in `tests/db/`).

**Files:** Create `tests/db/test_resource_kind_parity.py`.

- [ ] **Step 1: Write the test**

```python
# tests/db/test_resource_kind_parity.py
"""CHECK<->registry parity: every resources_kind_check kind has a runtime (ADR-0071)."""

from __future__ import annotations

import re

import psycopg

from kdive.db import migrate
from kdive.domain.models import ResourceKind
from kdive.providers.composition import build_provider_resolver


def _check_allowed_kinds(conn: psycopg.Connection) -> set[str]:
    """Read the kinds admitted by the live resources_kind_check constraint."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'resources_kind_check'"
    ).fetchone()
    assert row is not None, "resources_kind_check constraint is missing"
    # pg renders the CHECK as ... ARRAY['local-libvirt'::text, ...]; the single-quoted
    # literals are exactly the admitted kinds (the ::text casts sit outside the quotes).
    return set(re.findall(r"'([^']+)'", row[0]))


def test_every_check_allowed_kind_has_a_registered_runtime(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    assert allowed == {"local-libvirt"}  # the CHECK widen to fault-inject lands in issue 2
    resolver = build_provider_resolver()
    buildable = {k.value for k in resolver.registered_kinds()}
    # Every kind the DB will admit must resolve to a runtime (no admit-then-throw drift).
    assert allowed <= buildable
    for kind in allowed:
        assert resolver.resolve(ResourceKind(kind)) is not None


def test_every_registered_kind_is_check_allowed(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    resolver = build_provider_resolver()
    # No runtime for a kind the DB forbids (discovery insert would fail otherwise).
    for kind in resolver.registered_kinds():
        assert kind.value in allowed
```

- [ ] **Step 2: Run it, expect pass (with Docker) or clean skip (without)**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_resource_kind_parity.py -q` → PASS (needs Docker; CI sets this flag).
Without Docker: `uv run python -m pytest tests/db/test_resource_kind_parity.py -q` → SKIPPED.

- [ ] **Step 3: Commit**

```bash
git add tests/db/test_resource_kind_parity.py
git commit -m "test(providers): assert CHECK<->registry parity for resource kinds"
```

---

## Task 6: Full guardrails + final sweep

- [ ] **Step 1: Format + lint + type**

Run: `just lint && just type`
Expected: clean. If any warning remains, fix it (zero-warnings policy). The `ProviderRuntime` import in `systems.py`/`control.py`/`vmcore.py` and the `TYPE_CHECKING` `ProviderRuntime` in `__main__.py` should already be gone (Tasks 3–4); confirm none lingers (ruff F401).

- [ ] **Step 2: Full test suite**

Run: `just test`
Expected: green (Docker-gated db/integration tests run when Docker is present; otherwise skip cleanly — they must not fail).

- [ ] **Step 3: Confirm no behavioral diff under the provider**

Run: `git diff --stat main..HEAD -- src/kdive/providers/local_libvirt/` → **empty** (acceptance: no behavioral diff under `providers/local_libvirt/*`).

- [ ] **Step 4: Confirm no new migration**

Run: `git diff --name-only main..HEAD -- src/kdive/db/schema/` → **empty** (No DDL — the CHECK widen is issue 2).

- [ ] **Step 5: Commit any lint/type fixups (if Step 1 changed anything)**

```bash
git add -A
git commit -m "chore(providers): satisfy lint/type after resolver threading"
```

---

## Self-Review

**Spec coverage (issue #180 acceptance):**
- `ResourceKind.FAULT_INJECT` enum member → Task 1. ✓
- Composition map per deployment + opt-in gate (fault-inject absent from default prod) → Task 3. ✓
- `kind`-keyed resolver threaded to worker handlers (per-op, Task 4) + post-System MCP boundary (via `app.py` resolving the local runtime, Task 3; per-target MCP resolution deferred to issues 2/4 per the confirmed decision). ✓
- Resolution scoped to post-System ops; pre-grant allocation plane and discovery do not resolve a runtime (discovery fans out over the map's own kinds via `register_all_discovery`; `allocations.*` untouched) → Tasks 2–4. ✓
- Unknown kind → `configuration_error`, fail closed → Task 2 (`resolve`). ✓
- CHECK↔registry parity test over local-libvirt only → Task 5. ✓
- No DDL → verified in Task 6 Step 4. ✓
- local-libvirt behavior unchanged; only wiring changes → handler signatures preserve positional port injection; MCP registrar modules untouched; verified in Task 6 Step 3. ✓

**Green-at-every-commit:** Task 1 (enum) and Task 2 (additive module) are self-contained. Task 3 renames `build_default_provider_runtime` **and** updates all three top-level callers (`app.py`, `__main__`, `bootstrap`) **and** the three tests that referenced the old name in one commit, with verification that runs `tests/mcp/core/test_app.py` (import + binding). Task 4 changes all four planes' `register_handlers` **and** `app.py`'s `_HANDLER_REGISTRARS` in one commit, again verified against `test_app.py`, and removes the orphaned `ProviderRuntime` imports so `just lint` stays clean. No intermediate commit leaves a renamed symbol or a changed signature with an un-updated caller.

**Placeholder scan:** none — every code step shows the code.

**Type/name consistency:** `ProviderResolver`, `build_provider_resolver`, `build_local_runtime`, `register_all_discovery`, `runtime_for_system`, `runtime_for_run`, `resolve`, `registered_kinds`, `enable_fault_inject`; kwarg `resolver=` (handlers/`register_handlers`) and `provider_resolver=` (`build_app`/`build_handler_registry`) used consistently across tasks.

**Risk note:** the only behavior-sensitive change is moving port acquisition inside the handlers (Task 4). Each resolution is placed *after* the existing existence/precheck guard and *immediately before* the first provider call (e.g. `build` resolves inside `if result is None`; `teardown` resolves after the locked block, before `provisioning.teardown`), so gone-target idempotency (`teardown` returns `None`), the early-return short-circuits (existing build result, existing same-method vmcore key, terminal force-crash target), and the "target is gone" `infrastructure_failure` category are all preserved unchanged.
