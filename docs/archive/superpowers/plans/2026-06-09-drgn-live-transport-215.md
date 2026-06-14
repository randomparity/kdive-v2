# drgn-live Transport Generalization Implementation Plan (#215)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make remote in-guest drgn reachable end-to-end by renaming the live-introspection transport from the mechanism token `ssh` to the capability token `drgn-live`, with profile-derived credential resolution and the remote bare-domain handle contract.

**Architecture:** A capability-named transport token (`drgn-live`) realized per provider (local over SSH, remote over the qemu-guest-agent). Core (`mcp/tools/debug/`) is generalized off the ssh-credential + ssh-string assumptions; the credential need moves to a `profiles/` predicate; the handle *scheme* (`TransportHandleData.kind`) stays a provider-internal realization detail distinct from the agent-facing *token*. Spec: `docs/superpowers/specs/2026-06-09-drgn-live-transport-design.md`. ADR: `docs/adr/0085-drgn-live-transport-generalization.md`.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`. Guardrails: `just lint`, `just type`, `just test`, `just docs-check`, `just m2-gate`, `just check-mermaid`.

**Key distinction carried by every task:**
- **Transport token** (`{gdbstub, drgn-live}`): the `transport=` arg, `debug_sessions.transport`, the `kind` passed to `open_transport`, the single-attach conflict key. Owned by `sessions.py _TRANSPORTS`, the `introspect.run` gate, and each connector's accepted-kind check.
- **Handle scheme** (`{gdbstub, ssh, drgn-live}`): `TransportHandleData.kind`, validated on decode against `providers/ports/lifecycle.py _TRANSPORT_KINDS`. Local emits `ssh://`, fault-inject emits `drgn-live://`, remote emits a **bare domain name** (unschemed). Never drop `ssh` here.

**Execution mode: inline.** This is one tightly-coupled rename — renaming the token in `sessions.py`/`introspect.py` breaks the connector tests and the shared debug-test scaffolding at the same time, so the tasks are NOT independent and a context-free subagent-per-task is the wrong tool. Execute these tasks **inline in one session** (`superpowers:executing-plans`). Where a task's test constructs a connector or reuses a fixture, **read that connect/test module first and mirror its nearest existing `gdbstub` test** — do not invent constructor signatures. The `...` in the test snippets below mark exactly those seams to copy from the real module.

**Guardrail commands (run before every commit):**
```
uv run python -m pytest <focused test> -q          # the focused test for the task
uv run python -m pytest tests/mcp/debug tests/providers -q   # the affected suites — every commit
just lint                                           # ruff check + ruff format --check
just type                                           # ty check, whole tree
just m2-gate                                        # portability gate — run POST-commit (reads pre-M2..HEAD, committed only)
```
The CI hard gate is the full `just test`; run it before the Tasks 6, 7, and 8 commits (the ones that rename shared scaffolding or touch core), not only at Task 8. `just m2-gate` measures committed history (`pre-M2..HEAD`), so a pre-commit run reports the previous commit's state — always run it **after** the commit it is meant to validate.

---

## Task 1: profiles predicate `drgn_live_requires_credential`

**Files:**
- Modify: `src/kdive/profiles/provisioning.py` (add a function beside `ssh_credential_ref`, ~line 413)
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/profiles/test_provisioning.py` (match the existing import of `ProvisioningProfile` and any profile fixtures in that file; build minimal valid profiles inline):

```python
from kdive.profiles.provisioning import drgn_live_requires_credential


def _local_profile_doc() -> dict:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2, "memory_mb": 2048, "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": "/x.qcow2"}}},
    }


def _remote_profile_doc() -> dict:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2, "memory_mb": 2048, "disk_gb": 10,
        "boot_method": "disk-image",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {"remote-libvirt": {"base_image_volume": "base-fedora40"}},
    }


def test_drgn_live_requires_credential_true_for_local_section() -> None:
    profile = ProvisioningProfile.parse(_local_profile_doc())
    assert drgn_live_requires_credential(profile) is True


def test_drgn_live_requires_credential_false_for_remote_section() -> None:
    profile = ProvisioningProfile.parse(_remote_profile_doc())
    assert drgn_live_requires_credential(profile) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -k drgn_live_requires_credential -q`
Expected: FAIL with `ImportError: cannot import name 'drgn_live_requires_credential'`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/profiles/provisioning.py`, immediately after `ssh_credential_ref` (ends ~line 416):

```python
def drgn_live_requires_credential(profile: ProvisioningProfile) -> bool:
    """Return whether this profile's drgn-live transport needs a core-resolved credential.

    True for a local-libvirt section (drgn-live is realized over SSH, ADR-0039), False
    otherwise (remote reaches the guest agent over qemu+tls; fault-inject is synthetic).
    Keeps the credential decision provider-agnostic in core, which only asks this predicate
    (ADR-0085 Decision 2).
    """
    return profile.provider.local_libvirt_section is not None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -k drgn_live_requires_credential -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
just lint && just type
git add src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py
git commit -m "feat: add drgn_live_requires_credential profile predicate (#215)"
```

---

## Task 2: handle-scheme decode set accepts `drgn-live`

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py:15`
- Test: `tests/providers/test_ports_lifecycle.py` (create if absent; otherwise add to the existing transport-handle test module)

- [ ] **Step 1: Write the failing test**

Find the existing test for `TransportHandleData` (grep `TransportHandleData` under `tests/`); add there, or create `tests/providers/test_ports_lifecycle.py`:

```python
from kdive.providers.ports import TransportHandleData


def test_decode_accepts_drgn_live_scheme() -> None:
    decoded = TransportHandleData.decode("drgn-live://127.0.0.1:1234")
    assert decoded.kind == "drgn-live"
    assert decoded.host == "127.0.0.1"
    assert decoded.port == 1234


def test_decode_still_accepts_ssh_scheme() -> None:
    assert TransportHandleData.decode("ssh://127.0.0.1:22").kind == "ssh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/test_ports_lifecycle.py -q`
Expected: `test_decode_accepts_drgn_live_scheme` FAILS — decode raises `configuration_error` ("no known transport scheme") because `drgn-live` is not yet in `_TRANSPORT_KINDS`.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/providers/ports/lifecycle.py:15`:

```python
# The handle-scheme decode set (TransportHandleData.kind), NOT the agent-facing transport
# token set. Connectors emit: gdbstub (all), ssh (local drgn-live realization), drgn-live
# (fault-inject). Remote drgn-live emits a bare domain name (unschemed) — never decoded here.
_TRANSPORT_KINDS = frozenset({"gdbstub", "ssh", "drgn-live"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/test_ports_lifecycle.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
just lint && just type
git add src/kdive/providers/ports/lifecycle.py tests/providers/test_ports_lifecycle.py
git commit -m "feat: accept the drgn-live handle scheme on decode (#215)"
```

---

## Task 3: local connector accepts the `drgn-live` kind (keeps the `ssh://` handle scheme)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/connect.py` (constants ~28-29; `open_transport` ~81-98; `_open_ssh` ~120-138)
- Test: `tests/providers/local_libvirt/` (the connect test module — grep `open_transport` under that dir)

- [ ] **Step 1: Write the failing test**

In the local connect test module, add (mirror the existing gdbstub/ssh open tests, injecting the same seams they use — the endpoint resolver + ssh-connect probe):

```python
def test_open_transport_accepts_drgn_live_kind_and_emits_ssh_scheme_handle() -> None:
    connect = LocalLibvirtConnect(
        resolve_endpoint=lambda system: ("127.0.0.1", 1234),
        probe=lambda host, port: True,
        resolve_ssh_endpoint=lambda system: ("127.0.0.1", 22),
        ssh_connect=lambda host, port: True,
    )
    handle = connect.open_transport(SystemHandle("kdive-x"), "drgn-live")
    assert str(handle).startswith("ssh://")  # realization detail; scheme stays ssh
    connect.close_transport(handle)  # round-trips (ssh:// decodes)
```

> Match the real `LocalLibvirtConnect.__init__` seam names from the file; the four seams above are the loopback-endpoint resolver, RSP probe, ssh-endpoint resolver, and ssh reachability probe. If construction differs, use the module's existing test constructor/factory.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/ -k drgn_live -q`
Expected: FAIL — `open_transport` raises `unsupported transport kind: 'drgn-live'` (it still matches `_SSH = "ssh"`).

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/providers/local_libvirt/lifecycle/connect.py`, split the kind constant from the scheme constant:

```python
_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"   # the accepted transport kind (ADR-0085)
_SSH_SCHEME = "ssh"        # the handle scheme local emits (its realization)
```

In `open_transport` (~81-98):

```python
def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
    """Open a single-attach transport (gdbstub or drgn-live) and return its handle."""
    if kind == _GDBSTUB:
        return self._open_gdbstub(system)
    if kind == _DRGN_LIVE:
        return self._open_ssh(system)
    raise _config_error(f"unsupported transport kind: {kind!r}")
```

In `_open_ssh` (~138), keep the handle scheme `ssh`:

```python
    return TransportHandle(TransportHandleData(kind=_SSH_SCHEME, host=host, port=port).encode())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/ -k "drgn_live or transport" -q`
Expected: PASS (the new test and the existing gdbstub test; update any existing test that called `open_transport(..., "ssh")` to `"drgn-live"`).

- [ ] **Step 5: Commit**

```bash
just lint && just type
git add src/kdive/providers/local_libvirt/lifecycle/connect.py tests/providers/local_libvirt/
git commit -m "feat: local connector accepts the drgn-live kind (#215)"
```

---

## Task 4: remote connector adds the `drgn-live` branch + decode-or-no-op close

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/connect.py` (constants ~22; `open_transport` ~51-82; `close_transport`)
- Test: `tests/providers/remote_libvirt/` (the connect test module)

- [ ] **Step 1: Write the failing test**

```python
def test_open_transport_drgn_live_returns_bare_domain_handle() -> None:
    connect = RemoteLibvirtConnect(config_factory=lambda: _cfg(), resolve_port=..., probe=...)
    handle = connect.open_transport(SystemHandle("kdive-remote-1"), "drgn-live")
    assert str(handle) == "kdive-remote-1"  # ADR-0083 §4: bare domain, no scheme


def test_close_transport_no_ops_on_bare_domain_handle() -> None:
    connect = RemoteLibvirtConnect(config_factory=lambda: _cfg(), resolve_port=..., probe=...)
    connect.close_transport(TransportHandle("kdive-remote-1"))  # must not raise


def test_close_transport_still_validates_schemed_gdbstub_handle() -> None:
    connect = RemoteLibvirtConnect(config_factory=lambda: _cfg(), resolve_port=..., probe=...)
    connect.close_transport(TransportHandle("gdbstub://10.0.0.5:1234"))  # decodes, no-ops
```

> Reuse the module's existing `RemoteLibvirtConnect` test constructor and `_cfg()`/config fixture (the one the gdbstub tests use). The drgn-live `open_transport` path must NOT require `gdb_addr` (it returns the domain without touching config), so the test needs no gdb_addr set.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/ -k "drgn_live or bare_domain" -q`
Expected: FAIL — `open_transport` raises `unsupported transport kind: 'drgn-live'`, and `close_transport` raises `configuration_error` on the bare domain.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/providers/remote_libvirt/connect.py`, add the kind constant:

```python
_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"
```

In `open_transport` (~51), add the drgn-live branch BEFORE the gdbstub-only logic:

```python
def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
    if kind == _DRGN_LIVE:
        # In-guest drgn rides the guest agent keyed by domain; the handle IS the domain
        # name core derived (ADR-0083 §4). No gdbstub host/port, no probe.
        return TransportHandle(str(system))
    if kind != _GDBSTUB:
        raise _config_error(f"unsupported transport kind: {kind!r}")
    # ... existing gdbstub body unchanged ...
```

Replace `close_transport`:

```python
def close_transport(self, handle: TransportHandle) -> None:
    """No-op close. A schemed gdbstub handle is validated; the bare-domain drgn-live
    handle (ADR-0083 §4) is connectionless and needs no validation."""
    if "://" in str(handle):
        TransportHandleData.decode(handle)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/remote_libvirt/ -k "drgn_live or bare_domain or gdbstub" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/connect.py tests/providers/remote_libvirt/
git commit -m "feat: remote connector drgn-live branch + bare-domain close (#215)"
```

---

## Task 5: fault-inject connector accepts the `drgn-live` kind

**Files:**
- Modify: `src/kdive/providers/fault_inject/lifecycle/provider.py:46`
- Test: `tests/providers/fault_inject/` (the provider/connect test module)

- [ ] **Step 1: Write the failing test**

```python
def test_fault_inject_open_close_drgn_live_round_trips() -> None:
    connect = FaultInjectConnect()  # match the real constructor
    handle = connect.open_transport(SystemHandle("sys-1"), "drgn-live")
    assert str(handle).startswith("drgn-live://")
    connect.close_transport(handle)  # decode of drgn-live:// must succeed (Task 2)


def test_fault_inject_rejects_legacy_ssh_kind() -> None:
    connect = FaultInjectConnect()
    with pytest.raises(CategorizedError):
        connect.open_transport(SystemHandle("sys-1"), "ssh")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/fault_inject/ -k "drgn_live or legacy_ssh" -q`
Expected: FAIL — `open_transport(..., "drgn-live")` raises `unknown transport kind 'drgn-live'` (set still has `ssh`, not `drgn-live`).

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/providers/fault_inject/lifecycle/provider.py:46`:

```python
_TRANSPORT_KINDS = frozenset({"gdbstub", "drgn-live"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/fault_inject/ -k "drgn_live or legacy_ssh or transport" -q`
Expected: PASS (update any existing fault-inject test that opened `"ssh"` to `"drgn-live"`).

- [ ] **Step 5: Commit**

```bash
just lint && just type
git add src/kdive/providers/fault_inject/lifecycle/provider.py tests/providers/fault_inject/
git commit -m "feat: fault-inject connector accepts the drgn-live kind (#215)"
```

---

## Task 6: core `sessions.py` — rename token + profile-derived credential

**Files:**
- Modify: `src/kdive/mcp/tools/debug/sessions.py` (constants 60-62; `_credential_backend` 309-312; `_resolve_credential` 353-384; docstrings 244-247)
- Modify: `scripts/m2_portability_gate.py` (`ALLOWED_FILES`)
- Test: `tests/mcp/debug/test_debug_tools.py`

- [ ] **Step 1: Write the failing tests**

In `tests/mcp/debug/test_debug_tools.py`, migrate the existing `ssh` suite token `"ssh"` → `"drgn-live"` (the `# --- debug.start_session(transport="ssh")` block ~592-960, the `_seed_session` and `_FakeConnector` `port = 22 if ... == "ssh"` → `== "drgn-live"`), and ADD the remote case. New helpers + tests:

```python
def _remote_profile() -> dict[str, Any]:
    return {
        "schema_version": 1, "arch": "x86_64",
        "vcpu": 4, "memory_mb": 4096, "disk_gb": 20,
        "boot_method": "disk-image",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {"remote-libvirt": {"base_image_volume": "base-fedora40"}},
    }


class _DomainHandleConnector(_FakeConnector):
    """Mimics the remote connector: drgn-live returns the bare SystemHandle (domain)."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        self.opened.append((str(system), kind))
        if kind == "drgn-live":
            return TransportHandle(str(system))
        return super().open_transport(system, kind)


def test_start_session_drgn_live_remote_skips_credential_and_stores_domain_handle(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_profiled_system(pool, alloc_id, _remote_profile())
            run_id = await _seed_run(pool, sys_id)
            connector = _DomainHandleConnector()
            # No secret_backend supplied: a remote drgn-live start must not need one.
            resp = await _start_session(
                pool, _ctx(), run_id=run_id, transport="drgn-live", connector=connector
            )
            assert resp.status == "live"
            assert connector.opened == [("kdive-x", "drgn-live")]
            async with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT transport, transport_handle FROM debug_sessions WHERE id = %s",
                    (resp.object_id,),
                )
                row = await cur.fetchone()
        assert row is not None
        assert row["transport"] == "drgn-live"
        assert row["transport_handle"] == "kdive-x"  # bare domain, ADR-0083 §4

    asyncio.run(_run())


def test_start_session_drgn_live_local_missing_ref_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)  # local, no ssh ref
            run_id = await _seed_run(pool, sys_id)
            resp = await _start_session(
                pool, _ctx(), run_id=run_id, transport="drgn-live",
                connector=_FakeConnector(), secret_backend=_OrderRecordingBackend([]),
            )
            assert resp.status == "failed"
            assert resp.error_category is ErrorCategory.CONFIGURATION_ERROR
            assert resp.data.get("reason") == "ssh_credential_ref_missing"

    asyncio.run(_run())
```

Also rename the existing `_ssh_profile`-based tests' transport arg to `"drgn-live"` and keep asserting `row["transport_handle"].startswith("ssh://")` (local realization scheme is unchanged) and `connector.opened == [("kdive-x", "drgn-live")]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -k "drgn_live" -q`
Expected: FAIL — `start_session` rejects `transport="drgn-live"` (`_TRANSPORTS` still `{gdbstub, ssh}` → `_config_error`).

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/mcp/tools/debug/sessions.py`:

Constants (60-62):

```python
_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"
_TRANSPORTS = frozenset({_GDBSTUB, _DRGN_LIVE})
```

Import the predicate (with the existing `ssh_credential_ref` import, ~line 50):

```python
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    drgn_live_requires_credential,
    ssh_credential_ref,
)
```

`_credential_backend` (309-312):

```python
def _credential_backend(self, session_id: UUID, transport: str) -> SecretBackend | None:
    if transport != _DRGN_LIVE or self._secret_backend_factory is None:
        return None
    return self._secret_backend_factory(session_id)
```

`_resolve_credential` (353-384) — gate on the predicate, not the transport string:

```python
def _resolve_credential(
    system: System, transport: str, secret_backend: SecretBackend | None
) -> None | ToolResponse:
    """Resolve + register the SSH credential before transport use (ADR-0039 §2 ordering).

    A credential is needed only for a drgn-live transport whose profile realizes it over SSH
    (the local-libvirt section; ADR-0085 Decision 2). Returns ``None`` when none is required
    (gdbstub, or a guest-agent realization) or resolution succeeded, else a failure envelope.
    """
    if transport != _DRGN_LIVE:
        return None
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    if not drgn_live_requires_credential(profile):
        return None
    ref = ssh_credential_ref(profile)
    if ref is None:
        return _config_error(str(system.id), data={"reason": "ssh_credential_ref_missing"})
    if secret_backend is None:
        return ToolResponse.failure(str(system.id), ErrorCategory.MISSING_DEPENDENCY)
    try:
        secret_backend.resolve(ref)
    except PathSafetyError:
        return ToolResponse.failure(str(system.id), ErrorCategory.CONFIGURATION_ERROR)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    return None
```

Update the `start_session` docstring (244-247) `transport="ssh"` → `transport="drgn-live"`, the `transport` Field description (586) `ssh` → `drgn-live`, and the module docstring/comment references (e.g. the `_OCCUPIED_SQL` comment at 68-70 "a gdbstub and an ssh session" → "a gdbstub and a drgn-live session").

Add the two core files to `scripts/m2_portability_gate.py` `ALLOWED_FILES`:

```python
        # drgn-live transport generalization (#215, ADR-0085): the deliberate core touch.
        "src/kdive/mcp/tools/debug/sessions.py",
        "src/kdive/mcp/tools/debug/introspect.py",
```

- [ ] **Step 4: Run tests (full affected suites — this commit renames shared scaffolding)**

Run: `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q`
Expected: PASS (migrated suite + new remote/local cases).
Run: `just lint && just type && just test`
Expected: full suite green. The shared `_FakeConnector`/`_seed_session` fakes live in this module; other debug/reconciler/integration modules use `gdbstub` and are unaffected, but confirm the full run before committing a core touch.

- [ ] **Step 5: Commit, then validate the gate POST-commit**

```bash
git add src/kdive/mcp/tools/debug/sessions.py scripts/m2_portability_gate.py tests/mcp/debug/test_debug_tools.py
git commit -m "feat: route drgn-live start_session off the ssh assumption (#215)"
just m2-gate   # post-commit: confirms the now-committed sessions.py touch is allowlisted
```
Expected: `gate passed` — `sessions.py` is touched and allowlisted (introspect.py is allowlisted here too, touched in Task 7).

---

## Task 7: core `introspect.py` — gate on `drgn-live`

**Files:**
- Modify: `src/kdive/mcp/tools/debug/introspect.py` (constants 44-45; `LiveSshSession` 87-93; `resolve_live_ssh_session` 95-114; gate 110; tool descriptions 229, 240; docstrings)
- Test: `tests/mcp/debug/test_introspect_tools.py`

- [ ] **Step 1: Write the failing tests**

Migrate `tests/mcp/debug/test_introspect_tools.py`: rename `_seed_live_ssh_session` → `_seed_live_drgn_session` with `transport: str = "drgn-live"`, and the `port = 22 if transport == "ssh" else 1234` line in its handle seeding to `== "drgn-live"`. The negative `test_run_live_non_*_session_is_config_error` that seeds `transport="gdbstub"` stays (a gdbstub session must still be rejected by `introspect.run`). Add a remote-domain-handle routing test:

```python
def test_run_live_routes_bare_domain_handle_to_introspector(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, transport_handle="kdive-remote-1")
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_run(
                pool, _live_ctx(), session_id=session_id, helper="tasks", introspector=port
            )
            assert resp.status == "succeeded"
            assert port.calls and port.calls[0]["transport_handle"] == "kdive-remote-1"

    asyncio.run(_run())
```

> Extend `_seed_live_drgn_session` to accept `transport_handle: str | None = None`; when given, store it verbatim (the bare domain) instead of the `TransportHandleData(...)`-encoded form. Extend `_FakeLiveIntrospector` to record `calls` (a list of the kwargs it was called with) if it does not already.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/debug/test_introspect_tools.py -k "drgn or bare_domain or non_" -q`
Expected: FAIL — the live tests gate on `session.transport != "ssh"`, so a `drgn-live` session is rejected (`configuration_error`) and the happy path fails.

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/mcp/tools/debug/introspect.py`:

Constant (45): `_DRGN_LIVE = "drgn-live"` (replace `_SSH = "ssh"`).

Rename `LiveSshSession` → `LiveDrgnSession` (87) and `resolve_live_ssh_session` → `resolve_live_drgn_session` (95); update both call sites (146, 243) and the `_session_config_error` message. The gate (110):

```python
    if session.state is not DebugSessionState.LIVE or session.transport != _DRGN_LIVE:
        raise _session_config_error()
```

Update tool Field descriptions: `introspect.run`'s `session_id` description (229) "A live ssh DebugSession" → "A live drgn-live DebugSession"; the docstrings on `resolve_live_drgn_session` (98-102) and `introspect_run` (132-139) "ssh transport"/"ssh session" → "drgn-live transport". Keep `_session_config_error`'s message provider-neutral, e.g. "debug session does not resolve to a live drgn-live session".

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/debug/test_introspect_tools.py -q`
Expected: PASS.
Run: `just lint && just type && just test`
Expected: full suite green (this commit touches core `introspect.py`).

- [ ] **Step 5: Commit, then validate the gate POST-commit**

```bash
git add src/kdive/mcp/tools/debug/introspect.py tests/mcp/debug/test_introspect_tools.py
git commit -m "feat: gate introspect.run on the drgn-live transport (#215)"
just m2-gate   # post-commit: introspect.py touched + allowlisted (from Task 6)
```
Expected: `gate passed`.

---

## Task 8: regenerate the tool reference + full-suite sweep

**Files:**
- Modify: `docs/guide/reference/*` (generated)
- Sweep: any remaining `transport="ssh"` / `_seed_live_ssh_session` / `_seed_session(..., transport="ssh")` references in `tests/`

- [ ] **Step 1: Find any stragglers**

Run: `rg -n "transport=\"ssh\"|transport='ssh'|_seed_live_ssh_session|live ssh|== \"ssh\"" src tests`
Expected: only intentional handle-scheme uses (`TransportHandleData(kind="ssh"...)` in local realization/tests) remain; fix any leftover token uses to `drgn-live`.

- [ ] **Step 2: Regenerate the committed tool reference**

Run: `just docs`
Then: `just docs-check`
Expected: `docs-check` PASS (no diff) after regeneration; the `debug.start_session` `transport` description and `introspect.run` `session_id` description now read `drgn-live`.

- [ ] **Step 3: Run the full guardrail suite**

Run: `just lint && just type && just test && just m2-gate && just check-mermaid`
Expected: all green. Investigate and fix any failure before committing.

- [ ] **Step 4: Commit**

```bash
git add docs/guide/reference tests
git commit -m "docs: regenerate tool reference for the drgn-live transport (#215)"
```

---

## Self-review checklist (run after all tasks)

- **Spec coverage:** token rename (Tasks 3-7), profile predicate (Task 1), remote bare-domain handle + close (Task 4), decode set (Task 2), fault-inject (Task 5), gate allowlist (Task 6), docs regen (Task 8), remote no-probe (inherent — remote `open_transport` does no IO, Task 4), gdb-MI cross-tier behavior (documented, deliberately unguarded — no task).
- **Token vs scheme:** `sessions.py _TRANSPORTS = {gdbstub, drgn-live}` (token) ≠ `ports/lifecycle.py _TRANSPORT_KINDS = {gdbstub, ssh, drgn-live}` (scheme). Verify both are exactly these sets.
- **end_session round-trips:** local (`ssh://` decodes), fault-inject (`drgn-live://` decodes), remote (bare domain no-ops) — covered by Tasks 3/5/4 close tests.
- **No new ErrorCategory / tool / migration:** confirmed; only `ALLOWED_FILES` grows.

## Rollback / cleanup

Each task is a single commit; revert in reverse order if needed. No schema migration, so no DB rollback. The portability gate stays green at every commit (Tasks 1-5 touch no core; Task 6 adds the allowlist entry in the same commit that first touches core).
