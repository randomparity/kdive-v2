# Remote connect/debug plane (#205) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the remote-libvirt connect/debug plane (issue #205, ADR-0083): direct-TCP gdbstub gdb-MI, in-guest drgn-live via the guest-agent seam, and worker-side vmcore postmortem — by first extracting the shared gdb-MI/drgn/RSP mechanics into a provider-neutral `providers/debug_common/` package.

**Architecture:** The worker-side gdb-MI engine, drgn report helpers, and RSP probe move out of `local_libvirt/` into `providers/debug_common/`, parametrized by a host-reachability policy (local: loopback-only; remote: ACL-remote, host = operator-config `gdb_addr`). The remote provider then supplies its own thin `Connector`, attach seam, and two introspectors over those shared seams. All changes are under `providers/` and `tests/providers/` — zero core touches, so the ADR-0076 portability gate stays green. Two core-coupled pieces (drgn-live MCP routing, dead-worker reconciler reset) are deferred to a follow-up.

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`. Guardrails: `just lint`, `just type`, `just test`, `just m2-gate`. Provider seam: typed `ProviderRuntime` ports (ADR-0063). All slow/host seams are injected and `live_vm`-gated; unit tests drive orchestration + error contracts with fakes (no libvirt host).

**Reference docs:** ADR-0083 (this issue), ADR-0079 (transport design), ADR-0076 (independence + gate), ADR-0078 (guest-agent seam), ADR-0080 (domain-XML gdbstub port), ADR-0032/0033/0034/0039 (the local planes being generalized).

**Conventions (every task):**
- Run `uv run python -m pytest <path> -q` for focused tests; `just lint && just type` before each commit.
- Conventional-commit subjects ≤72 chars, imperative; end every commit body with the trailer
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Errors are `CategorizedError(message, category=ErrorCategory.X, details={...})`; pick the most specific existing category — never invent strings.
- No relative imports; absolute `kdive.*` only. Ruff line length 100. Google-style docstrings on public APIs.
- This is a **behavior-preserving extraction** for Tasks 1–4: the moved code keeps its `# pragma: no cover - live_vm` markers; local-libvirt's existing tests must stay green unchanged (only their import paths update).

---

## File Structure

**New package `src/kdive/providers/debug_common/`:**
- `__init__.py` — re-exports the public shared symbols.
- `hostpolicy.py` — `HostPolicy` type + `require_loopback` (local SSRF gate) + `allow_acl_remote` (remote).
- `rsp.py` — `rsp_frame`, `valid_rsp_frame`, `rsp_reachable` (moved from `local_libvirt/lifecycle/connect.py`).
- `gdbmi.py` — the concrete `GdbMiEngine` + MI records + execution control + controllers (moved from `local_libvirt/debug/debug_gdbmi.py`), `attach` parametrized by `host_policy`.
- `introspect.py` — `assemble_report`, `helper_tasks/modules/sysinfo`, `_HELPERS`, byte-cap, `_Program`/`_Task`/`_Module` protocols (moved from `local_libvirt/debug/introspect_drgn.py`).

**New remote provider files:**
- `src/kdive/providers/remote_libvirt/connect.py` — `RemoteLibvirtConnect` (`Connector`): gdbstub direct-TCP.
- `src/kdive/providers/remote_libvirt/debug.py` — `remote_attach_seam` (ACL-remote gdb-MI attach).
- `src/kdive/providers/remote_libvirt/introspect.py` — `RemoteVmcoreIntrospect` (`from_vmcore`) + `RemoteLiveIntrospect` (`introspect_live`).

**Modified:**
- `src/kdive/providers/local_libvirt/lifecycle/connect.py`, `.../debug/debug_gdbmi.py`, `.../debug/introspect_drgn.py`, `.../debug/execution.py`, `.../debug/transcript.py` — re-point imports to `debug_common`; local `Connect` gains a defaulted `host_policy`.
- `src/kdive/providers/composition.py` — import the moved engine from `debug_common`; wire the remote ports.
- `src/kdive/providers/remote_libvirt/planes.py` — delete `UnimplementedConnector` + `UnimplementedIntrospector` (keep `UnimplementedController` + `UnimplementedRetriever`, which are issue #206).
- Test import-path updates in `tests/providers/local_libvirt/*` and `tests/mcp/debug/test_debug_ops.py`.

---

## Task 1: Host-reachability policy

**Files:**
- Create: `src/kdive/providers/debug_common/__init__.py`
- Create: `src/kdive/providers/debug_common/hostpolicy.py`
- Test: `tests/providers/debug_common/__init__.py` (empty), `tests/providers/debug_common/test_hostpolicy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/debug_common/test_hostpolicy.py
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.hostpolicy import allow_acl_remote, require_loopback


def test_require_loopback_accepts_loopback_literal():
    require_loopback("127.0.0.1")  # no raise
    require_loopback("::1")  # no raise


@pytest.mark.parametrize("host", ["10.0.0.5", "example.com", "", "0.0.0.0"])
def test_require_loopback_rejects_non_loopback(host):
    with pytest.raises(CategorizedError) as exc:
        require_loopback(host)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_allow_acl_remote_accepts_routable_literal_and_hostname():
    allow_acl_remote("10.0.0.5")  # no raise — operator-trusted gdb_addr
    allow_acl_remote("gdbhost.internal")  # no raise — hostname is allowed for remote


@pytest.mark.parametrize("host", ["", "   ", "has space", "a\tb"])
def test_allow_acl_remote_rejects_empty_or_malformed(host):
    with pytest.raises(CategorizedError) as exc:
        allow_acl_remote(host)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/debug_common/test_hostpolicy.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.debug_common.hostpolicy`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/providers/debug_common/hostpolicy.py
"""Host-reachability policy for the shared gdb-MI/RSP transport (ADR-0083 §2).

A `HostPolicy` validates a resolved RSP host before any network IO, or raises
`CONFIGURATION_ERROR`. `require_loopback` is the local SSRF control (the endpoint is
resolved from a libvirt domain, so a non-loopback host is rejected without DNS).
`allow_acl_remote` is the remote policy: the host is `RemoteLibvirtConfig.gdb_addr`,
operator-trusted config, so it need not be loopback — only non-empty and free of control
whitespace. The operator ACL restricting the unauthenticated gdbstub to the worker pool is
the security boundary (ADR-0079), not a host-shape assertion.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

from kdive.domain.errors import CategorizedError, ErrorCategory

type HostPolicy = Callable[[str], None]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


def require_loopback(host: str) -> None:
    """Raise unless `host` is a loopback IP literal (a hostname is rejected — no DNS)."""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise _config_error(f"RSP host must be a loopback IP literal, got {host!r}")


def allow_acl_remote(host: str) -> None:
    """Raise unless `host` is a non-empty, control-whitespace-free operator-config address."""
    if not host or host != host.strip() or any(c in host for c in " \t\r\n"):
        raise _config_error(f"remote gdbstub host must be a non-blank address, got {host!r}")
```

```python
# src/kdive/providers/debug_common/__init__.py
"""Provider-neutral worker-side debug mechanics shared across providers (ADR-0083)."""

from kdive.providers.debug_common.hostpolicy import (
    HostPolicy,
    allow_acl_remote,
    require_loopback,
)

__all__ = ["HostPolicy", "allow_acl_remote", "require_loopback"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/debug_common/test_hostpolicy.py -q`
Expected: PASS (7 cases).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/debug_common/ tests/providers/debug_common/
git commit -m "feat: add provider-neutral host-reachability policy (ADR-0083)"
```

---

## Task 2: Move the RSP codec to debug_common

**Files:**
- Create: `src/kdive/providers/debug_common/rsp.py`
- Modify: `src/kdive/providers/local_libvirt/lifecycle/connect.py`
- Modify: `tests/providers/local_libvirt/test_connect.py` (import path only)

The RSP codec (`rsp_frame`, `valid_rsp_frame`, `rsp_reachable`, plus the constants `_RSP_MAX_ACCUMULATE_BYTES` and `_PROBE_TIMEOUT_S`) currently lives in `connect.py:32-69,181-207`. Move it verbatim.

- [ ] **Step 1: Create `debug_common/rsp.py` with the moved code**

Copy `rsp_frame`, `valid_rsp_frame`, `rsp_reachable`, and the two module constants from
`local_libvirt/lifecycle/connect.py` into a new `src/kdive/providers/debug_common/rsp.py` with
this module docstring, keeping the `# pragma: no cover - live_vm` on `rsp_reachable`:

```python
"""RSP framing codec + bounded reachability probe (ported v1, ADR-0032/0083).

Shared by every provider's gdbstub Connect plane: `rsp_frame` builds a `$payload#xx`
packet, `valid_rsp_frame` validates a complete checksum-correct reply, and `rsp_reachable`
exchanges one read-only `?` halt-reason query and accepts only a valid frame (a stale or
non-RSP listener is rejected). The real socket path runs only under the `live_vm` gate.
"""
```

Add `import socket`, `import time` to the new module. Export via `__all__ = ["rsp_frame", "rsp_reachable", "valid_rsp_frame"]`.

- [ ] **Step 2: Re-point `connect.py` and delete the moved definitions**

In `local_libvirt/lifecycle/connect.py`: delete the moved function/constant definitions, and
replace the local `from kdive.providers.ports import ...` block's neighbours with:

```python
from kdive.providers.debug_common.rsp import rsp_frame, rsp_reachable, valid_rsp_frame
```

Keep `connect.py`'s `__all__` exporting `rsp_frame`, `rsp_reachable`, `valid_rsp_frame` (re-exported for the existing `tests/providers/local_libvirt/test_connect.py` import) **or** update the test import — see Step 3. Prefer updating the test (no re-export shim; global "replace, don't deprecate").

- [ ] **Step 3: Update the test import**

In `tests/providers/local_libvirt/test_connect.py`, change any
`from kdive.providers.local_libvirt.lifecycle.connect import rsp_frame, ...` to import the RSP
symbols from `kdive.providers.debug_common.rsp` (leave `LocalLibvirtConnect` imported from its
current module). Remove `rsp_frame`/`rsp_reachable`/`valid_rsp_frame` from `connect.py`'s
`__all__`.

- [ ] **Step 4: Run the local connect tests + the debug_common test**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_connect.py tests/providers/debug_common -q`
Expected: PASS, unchanged count for connect.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/debug_common/rsp.py src/kdive/providers/local_libvirt/lifecycle/connect.py tests/providers/local_libvirt/test_connect.py
git commit -m "refactor: move RSP codec to providers/debug_common (ADR-0083)"
```

---

## Task 3: Move the drgn report helpers to debug_common

**Files:**
- Create: `src/kdive/providers/debug_common/introspect.py`
- Modify: `src/kdive/providers/local_libvirt/debug/introspect_drgn.py`
- Modify: `tests/providers/local_libvirt/test_introspect_drgn.py` (import paths)

Move the **provider-neutral** drgn pieces from `introspect_drgn.py` into `debug_common/introspect.py`: the `_Task`, `_Module`, `_Program` protocols; `helper_tasks`, `_safe_stack`, `helper_modules`, `helper_sysinfo`, `_HELPERS`; `assemble_report`, `_byte_cap`, `_report_size`; the constants `_TASK_LIMIT`, `_BLOCKED_STATES`, `_REPORT_BYTE_CAP`; and the `_RunHelper`/`_Program`-typed aliases used by them. **Leave in `local_libvirt`**: `LocalLibvirtVmcoreIntrospect`, `LocalLibvirtLiveIntrospect`, `_real_fetch_object`, `_real_read_vmcore_build_id`, `_normalize_attach_error`, and the local-specific type aliases — these are local's wiring and re-import the shared pieces.

- [ ] **Step 1: Create `debug_common/introspect.py`**

Move the listed symbols verbatim under this docstring:

```python
"""Provider-neutral drgn report helpers + redact/byte-cap assembly (ADR-0033/0083).

The three fixed helpers (tasks, modules, sysinfo), the narrow drgn `_Program` surface they
operate on, and `assemble_report` (redact first — the single redaction boundary — then
byte-cap) are shared by every provider's vmcore and live introspectors. The real drgn
`Program` is adapted to `_Program` by each provider's `live_vm` seam.
"""
```

Export: `__all__ = ["assemble_report", "helper_modules", "helper_sysinfo", "helper_tasks"]`
(plus the protocols if any importer needs them — `_Program` is module-private, used via the type alias).

- [ ] **Step 2: Re-point `introspect_drgn.py`**

In `local_libvirt/debug/introspect_drgn.py`, delete the moved definitions and add:

```python
from kdive.providers.debug_common.introspect import (
    assemble_report,
    helper_modules,
    helper_sysinfo,
    helper_tasks,
)
```

If `LocalLibvirtVmcoreIntrospect`/`LocalLibvirtLiveIntrospect` reference `_Program`/`_RunHelper`
type aliases that moved, import them from `debug_common.introspect` too (re-declare the
`type _RunHelper = ...` alias locally if it is only used in local signatures). Keep
`introspect_drgn.py`'s `__all__` (it still exports the local classes + re-exports
`assemble_report`/`helper_*` for the existing test imports — but prefer updating the test in
Step 3 and exporting only the local classes).

- [ ] **Step 3: Update test imports**

In `tests/providers/local_libvirt/test_introspect_drgn.py`, point `assemble_report`/`helper_*`
imports at `kdive.providers.debug_common.introspect`; keep `LocalLibvirt*Introspect` imports at
their current module.

- [ ] **Step 4: Run the local introspect tests + debug_common**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_introspect_drgn.py tests/providers/debug_common -q`
Expected: PASS, unchanged count for introspect.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/debug_common/introspect.py src/kdive/providers/local_libvirt/debug/introspect_drgn.py tests/providers/local_libvirt/test_introspect_drgn.py
git commit -m "refactor: move drgn report helpers to providers/debug_common (ADR-0083)"
```

---

## Task 4: Move the gdb-MI engine to debug_common + parametrize the host policy

**Files:**
- Create: `src/kdive/providers/debug_common/gdbmi.py` (+ move `execution.py`, `transcript.py`, `mi` helpers if they are local-only deps of the engine — see Step 1)
- Modify: `src/kdive/providers/local_libvirt/debug/debug_gdbmi.py` (becomes local's `default_attach_seam` + `_resolve_debuginfo_ref` only, importing the engine from `debug_common`)
- Modify: `src/kdive/providers/composition.py` (import `GdbMiEngine` from `debug_common.gdbmi`)
- Modify: `tests/providers/local_libvirt/test_debug_gdbmi.py`, `test_mi_protocol.py`, `test_mi_controller.py`, `tests/mcp/debug/test_debug_ops.py` (import paths)

The concrete `GdbMiEngine` class and its private MI/execution/controller helpers move to
`debug_common/gdbmi.py`. Its dependencies (`execution.py`, `transcript.py`, the MiRecord
machinery) are provider-neutral too — move them alongside into `debug_common/` (e.g.
`debug_common/gdbmi.py` may absorb them, or keep `debug_common/execution.py` +
`debug_common/transcript.py`). The **only behavioral change** is the loopback gate becoming a
policy parameter.

- [ ] **Step 1: Move the engine + its neutral deps into debug_common**

First enumerate every importer of the symbols about to move, so Step 5 updates them all (the
`local_libvirt/debug/__init__.py` is a bare docstring with no re-exports, and the only non-test
src importer of `execution`/`transcript` is `debug_gdbmi.py` itself — but verify before trusting):

```bash
rg -ln "local_libvirt\.debug\.(debug_gdbmi|execution|transcript)|from kdive\.providers\.local_libvirt\.debug import" src tests
```

Record the exact `(file, symbol)` pairs this surfaces; Step 5 must update each one.

`git mv` the engine module and its neutral helper modules into `debug_common/`:

```bash
git mv src/kdive/providers/local_libvirt/debug/debug_gdbmi.py src/kdive/providers/debug_common/gdbmi.py
git mv src/kdive/providers/local_libvirt/debug/execution.py src/kdive/providers/debug_common/execution.py
git mv src/kdive/providers/local_libvirt/debug/transcript.py src/kdive/providers/debug_common/transcript.py
```

Then in `debug_common/gdbmi.py`: update its internal imports (`from kdive.providers.local_libvirt.debug.execution import ...` → `from kdive.providers.debug_common.execution import ...`, likewise transcript and the RSP/introspect moves). Update `execution.py`/`transcript.py` internal imports the same way. **Remove** from `gdbmi.py` the local-only `default_attach_seam` and `_resolve_debuginfo_ref` — they go back into local (Step 3). The module docstring's "Local-libvirt" framing becomes provider-neutral.

- [ ] **Step 2: Parametrize the host policy (the one behavioral change)**

In `debug_common/gdbmi.py`, change the `GdbMiEngine.__init__` signature to accept a policy
(default preserves local behavior) and replace `_validate_rsp_host` with the injected policy:

```python
from kdive.providers.debug_common.hostpolicy import HostPolicy, require_loopback

class GdbMiEngine:
    def __init__(
        self,
        controller_factory: ... = None,
        *,
        gdb_path_finder: Callable[[str], str | None] = shutil.which,
        redactor: Redactor | None = None,
        redactor_factory: Callable[[], Redactor] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        host_policy: HostPolicy = require_loopback,
    ) -> None:
        ...
        self._host_policy = host_policy
        ...
```

In `attach`, replace the line `self._validate_rsp_host(host)` with `self._host_policy(host)`,
and delete the `_validate_rsp_host` method. (Keep the `_mi_path` control-whitespace guard.)

- [ ] **Step 3: Rebuild local's `debug_gdbmi.py` as the local attach seam shim**

Create a new `src/kdive/providers/local_libvirt/debug/debug_gdbmi.py` that re-exports the engine
and keeps the local attach seam (unchanged behavior — default `require_loopback` policy):

```python
"""Local-libvirt gdb-MI wiring: the provider attach seam over the shared engine (ADR-0034/0083).

The gdb-MI engine itself is provider-neutral (`kdive.providers.debug_common.gdbmi`); this
module keeps only local-libvirt's `default_attach_seam` (loopback-only via the engine's default
host policy) and its `live_vm`-gated debuginfo resolver.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.ports import GdbMiAttachment


def _resolve_debuginfo_ref(run_id: str) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a Run's debuginfo object runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"run_id": run_id},
    )


def default_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:  # pragma: no cover - live_vm
    """The real `live_vm` local attach: resolve+materialize debuginfo, spawn gdb, connect RSP."""
    debuginfo_ref = _resolve_debuginfo_ref(run_id)
    del debuginfo_ref
    vmlinux_path = Path(tempfile.gettempdir()) / f"kdive-debuginfo-{run_id}"
    return GdbMiEngine().attach(
        host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
    )


__all__ = ["GdbMiEngine", "default_attach_seam"]
```

- [ ] **Step 4: Re-point `composition.py` imports**

In `composition.py`, change:

```python
from kdive.providers.debug_common.gdbmi import GdbMiEngine as LocalGdbMiEngine
from kdive.providers.local_libvirt.debug.debug_gdbmi import default_attach_seam
```

(the `default_attach_seam` import stays at the local module; only the engine import moves).

- [ ] **Step 5: Update test import paths**

Update every `(file, symbol)` pair the Step 1 grep surfaced. Concretely, in
`tests/providers/local_libvirt/test_debug_gdbmi.py`, `test_mi_protocol.py`,
`test_mi_controller.py`, and `tests/mcp/debug/test_debug_ops.py`, re-point each imported symbol to
its new home: the engine `GdbMiEngine`, the MI-record/parsing helpers, and the controller classes
→ `kdive.providers.debug_common.gdbmi`; `ExecutionControl` and any execution helper →
`kdive.providers.debug_common.execution`; `write_transcript`/transcript helpers →
`kdive.providers.debug_common.transcript`. `default_attach_seam` stays imported from the local
module. Do not guess — match each symbol to the module it was `git mv`'d into.

- [ ] **Step 6: Run the full providers + mcp suites (not just the named ones)**

Run: `uv run python -m pytest tests/providers tests/mcp -q`
Expected: PASS, unchanged counts (behavior-preserving). Running the full providers + mcp suites —
not only the three named ones — catches any importer the Step 1 grep missed within the same task
that moved the symbol, rather than four commits later at Task 10.

- [ ] **Step 7: Lint, type, commit**

```bash
just lint && just type
git add -A
git commit -m "refactor: move gdb-MI engine to providers/debug_common with host policy (ADR-0083)"
```

---

## Task 5: RemoteLibvirtConnect — gdbstub direct-TCP transport

**Files:**
- Create: `src/kdive/providers/remote_libvirt/connect.py`
- Test: `tests/providers/remote_libvirt/test_connect.py`

The connector composes `host = config.gdb_addr` (operator config; CONFIGURATION_ERROR if unset),
`port` from an injected `live_vm`-gated domain-XML reader, applies `allow_acl_remote`, probes RSP
with the shared `rsp_reachable`, and returns a `gdbstub://host:port` `TransportHandle`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/remote_libvirt/test_connect.py
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import SystemHandle, TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.connect import RemoteLibvirtConnect

_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


def _config(*, gdb_addr="10.0.0.5"):
    return RemoteLibvirtConfig(uri="qemu+tls://h/system", cert_refs=_REFS,
                               concurrent_allocation_cap=1, gdb_addr=gdb_addr)


def _connect(*, resolve_port, probe, config=None):
    return RemoteLibvirtConnect(
        config_factory=lambda: config or _config(),
        resolve_port=resolve_port,
        probe=probe,
    )


def test_open_gdbstub_returns_handle_for_reachable_stub():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    handle = c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    decoded = TransportHandleData.decode(handle)
    assert (decoded.kind, decoded.host, decoded.port) == ("gdbstub", "10.0.0.5", 47002)


def test_open_gdbstub_unset_gdb_addr_is_configuration_error():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True,
                 config=_config(gdb_addr=None))
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_open_gdbstub_unreachable_is_debug_attach_failure():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: False)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_open_gdbstub_socket_fault_is_transport_failure():
    def boom(host, port):
        raise OSError("connection refused")
    c = _connect(resolve_port=lambda system: 47002, probe=boom)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_unknown_kind_is_configuration_error():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "ssh")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_close_transport_validates_handle():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    c.close_transport(TransportHandleData(kind="gdbstub", host="10.0.0.5", port=47002).encode())
    with pytest.raises(CategorizedError):
        c.close_transport("not-a-handle")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_connect.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.remote_libvirt.connect`.

- [ ] **Step 3: Implement `RemoteLibvirtConnect`**

```python
# src/kdive/providers/remote_libvirt/connect.py
"""Remote-libvirt Connect plane: direct-TCP gdbstub transport (ADR-0079/0083).

`open_transport(system, "gdbstub")` composes the endpoint from operator config (the host is
`RemoteLibvirtConfig.gdb_addr`, the ACL'd listen address) and the per-System gdbstub port read
from the domain XML (ADR-0080), applies the ACL-remote host policy (no loopback gate — the host
is operator-trusted config, the operator ACL is the security boundary), probes RSP reachability,
and returns the encoded handle the gdb-MI tier consumes. The slow seams (domain-XML port read,
socket probe) are injected and `live_vm`-gated; orchestration and the full error contract are
unit-tested with fakes. `close_transport` validates the handle and no-ops (connectionless RSP).
"""

from __future__ import annotations

from collections.abc import Callable

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.debug_common.rsp import rsp_reachable
from kdive.providers.ports import SystemHandle, TransportHandle, TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env

_GDBSTUB = "gdbstub"

type _ResolvePort = Callable[[SystemHandle], int]
type _Probe = Callable[[str, int], bool]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


class RemoteLibvirtConnect:
    """The realized remote `Connector`: a single-attach direct-TCP gdbstub transport."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        resolve_port: _ResolvePort | None = None,
        probe: _Probe | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._resolve_port = resolve_port if resolve_port is not None else _real_resolve_port
        self._probe = probe if probe is not None else _real_probe

    @classmethod
    def from_env(cls) -> RemoteLibvirtConnect:
        """Build with the real `live_vm`-gated domain-XML reader + socket probe."""
        return cls()

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        """Open the gdbstub transport for `system`; raise for any other kind.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown kind, an unset
                ``gdb_addr``, or a malformed host; ``DEBUG_ATTACH_FAILURE`` if the stub does
                not answer RSP; ``TRANSPORT_FAILURE`` on a socket fault; ``MISSING_DEPENDENCY``
                propagated from the real domain-XML reader outside ``live_vm``.
        """
        if kind != _GDBSTUB:
            raise _config_error(f"unsupported transport kind: {kind!r}")
        config = self._config_factory()
        if not config.gdb_addr:
            raise _config_error("remote gdbstub host (KDIVE_REMOTE_LIBVIRT_GDB_ADDR) is unset")
        host = config.gdb_addr
        allow_acl_remote(host)
        port = self._resolve_port(system)
        try:
            reachable = self._probe(host, port)
        except OSError as exc:
            raise CategorizedError(
                "gdbstub transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "remote gdbstub did not answer RSP framing",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"host": host, "port": port},
            )
        return TransportHandle(TransportHandleData(kind=_GDBSTUB, host=host, port=port).encode())

    def close_transport(self, handle: TransportHandle) -> None:
        """Validate the handle, then no-op (connectionless RSP)."""
        TransportHandleData.decode(handle)


def _real_resolve_port(system: SystemHandle) -> int:  # pragma: no cover - live_vm
    raise CategorizedError(
        "reading a remote domain's recorded gdbstub port runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system": str(system)},
    )


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


__all__ = ["RemoteLibvirtConnect"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_connect.py -q`
Expected: PASS (6 cases).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/connect.py tests/providers/remote_libvirt/test_connect.py
git commit -m "feat: add RemoteLibvirtConnect gdbstub direct-TCP transport (ADR-0083)"
```

---

## Task 6: Remote gdb-MI attach seam

**Files:**
- Create: `src/kdive/providers/remote_libvirt/debug.py`
- Test: `tests/providers/remote_libvirt/test_debug.py`

The remote attach seam mirrors local's but builds the shared engine with the ACL-remote policy and
a remote debuginfo resolver. Both the resolver and the real attach are `live_vm`-gated, so the unit
test asserts the off-gate `MISSING_DEPENDENCY` contract (the orchestration that runs without a host).

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/remote_libvirt/test_debug.py
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.hostpolicy import allow_acl_remote, require_loopback
from kdive.providers.remote_libvirt.debug import remote_attach_seam


def test_remote_attach_seam_off_gate_reports_missing_dependency():
    # Off the live_vm gate the debuginfo resolver is a fail-closed seam; the seam surfaces
    # MISSING_DEPENDENCY before spawning gdb.
    with pytest.raises(CategorizedError) as exc:
        remote_attach_seam(host="10.0.0.5", port=47002, run_id="r1",
                           transcript_path=Path("/tmp/t.jsonl"))
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_remote_policy_accepts_non_loopback_but_loopback_policy_would_reject():
    allow_acl_remote("10.0.0.5")  # remote policy: OK
    with pytest.raises(CategorizedError):
        require_loopback("10.0.0.5")  # the local policy would reject — proves the inversion
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_debug.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.remote_libvirt.debug`.

- [ ] **Step 3: Implement the remote attach seam**

```python
# src/kdive/providers/remote_libvirt/debug.py
"""Remote-libvirt gdb-MI attach seam over the shared engine (ADR-0079/0083).

The gdb subprocess still runs on the worker; the only difference from local is the host policy
(ACL-remote, not loopback) and the debuginfo resolver (the remote build's vmlinux). Both the
resolver and the real attach are `live_vm`-gated, so off-gate the seam fails closed with
MISSING_DEPENDENCY and unit tests assert that contract.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.ports import GdbMiAttachment


def _resolve_remote_debuginfo_ref(run_id: str) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a remote Run's debuginfo object runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"run_id": run_id},
    )


def remote_attach_seam(
    *, host: str, port: int, run_id: str, transcript_path: Path
) -> GdbMiAttachment:
    """Resolve+materialize the remote debuginfo, spawn gdb, connect RSP (ACL-remote policy).

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` off the ``live_vm`` gate (the debuginfo
            resolver seam); ``DEBUG_ATTACH_FAILURE`` for a gdb/RSP attach fault on a live host.
    """
    debuginfo_ref = _resolve_remote_debuginfo_ref(run_id)  # fails closed off-gate
    del debuginfo_ref  # the live path fetches it to a temp file before attach
    vmlinux_path = Path(tempfile.gettempdir()) / f"kdive-remote-debuginfo-{run_id}"
    return GdbMiEngine(host_policy=allow_acl_remote).attach(
        host=host, port=port, vmlinux_path=vmlinux_path, transcript_path=transcript_path
    )


__all__ = ["remote_attach_seam"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_debug.py -q`
Expected: PASS (2 cases).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/debug.py tests/providers/remote_libvirt/test_debug.py
git commit -m "feat: add remote gdb-MI attach seam with ACL-remote policy (ADR-0083)"
```

---

## Task 7: RemoteVmcoreIntrospect — worker-side vmcore postmortem

**Files:**
- Create: `src/kdive/providers/remote_libvirt/introspect.py`
- Test: `tests/providers/remote_libvirt/test_introspect.py`

Mirror `LocalLibvirtVmcoreIntrospect`: fetch the core + vmlinux from the object store, verify the
core's build-id against the Run's recorded build-id, open drgn (`live_vm` seam), run the shared
helpers, redact + byte-cap. Off-gate (no drgn seam) it raises `MISSING_DEPENDENCY` before touching
the store.

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/remote_libvirt/test_introspect.py
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.providers.remote_libvirt.introspect import RemoteVmcoreIntrospect


class _FakeProgram:
    def iter_tasks(self): return []
    def iter_modules(self): return []
    def uts(self): return {"release": "6.1.0"}
    def boot_cmdline(self): return "ro"
    def cpus_online(self): return 1
    def mem_total_pages(self): return 1


def _vmcore_introspect(*, open_program=None, run_helper=None, fetch=None, build_id=lambda b: "BID"):
    return RemoteVmcoreIntrospect(
        fetch_object=fetch or (lambda ref: b"core" if "core" in ref else b"vmlinux"),
        read_vmcore_build_id=build_id,
        secret_registry=SecretRegistry(),
        open_program=open_program,
        run_helper=run_helper,
    )


def test_from_vmcore_off_gate_is_missing_dependency():
    introspect = _vmcore_introspect()  # no drgn seams
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_from_vmcore_build_id_mismatch_is_configuration_error():
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: {},
        build_id=lambda b: "OTHER",
    )
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_vmcore_returns_redacted_report():
    from kdive.providers.debug_common.introspect import (
        helper_modules, helper_sysinfo, helper_tasks,
    )
    helpers = {"tasks": helper_tasks, "modules": helper_modules, "sysinfo": helper_sysinfo}
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: helpers[name](prog),
    )
    out = introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert out.sysinfo["release"] == "6.1.0"
    assert out.truncated is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_introspect.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.providers.remote_libvirt.introspect`.

- [ ] **Step 3: Implement `RemoteVmcoreIntrospect`** (the `RemoteLiveIntrospect` class lands in Task 8 in the same file)

```python
# src/kdive/providers/remote_libvirt/introspect.py
"""Remote-libvirt introspection ports (ADR-0079/0083).

`RemoteVmcoreIntrospect` runs the offline drgn path on the worker (fetch core + vmlinux, verify
build-id provenance, run the shared helpers, redact + byte-cap) — no live reachability.
`RemoteLiveIntrospect` (Task 8) runs the in-guest drgn helper via the guest-agent seam. Both reuse
`debug_common.introspect.assemble_report` as the single redaction boundary. The drgn open/exec
paths are `live_vm`-gated; orchestration, provenance, and error contracts are unit-tested with
fakes.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.introspect import assemble_report
from kdive.providers.ports import IntrospectOutput
from kdive.security.secrets.secret_registry import SecretRegistry

_REPORT_BYTE_CAP = 1 << 20


class _Program(Protocol):
    def iter_tasks(self) -> list[object]: ...
    def iter_modules(self) -> list[object]: ...
    def uts(self) -> dict[str, str]: ...
    def boot_cmdline(self) -> str: ...
    def cpus_online(self) -> int: ...
    def mem_total_pages(self) -> int: ...


type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class RemoteVmcoreIntrospect:
    """Worker-side offline drgn introspection of a remote-captured vmcore (ADR-0033/0083)."""

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteVmcoreIntrospect:
        """Build from env; drgn seams left None (off-gate `from_vmcore` raises before any IO)."""
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=_real_read_vmcore_build_id,
            secret_registry=secret_registry,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` off the ``live_vm`` gate;
                ``CONFIGURATION_ERROR`` for a build-id provenance mismatch;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO; ``DEBUG_ATTACH_FAILURE`` if drgn
                cannot open the core.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected_build_id:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return assemble_report(
            tasks, modules, sysinfo,
            byte_cap=_REPORT_BYTE_CAP, secret_registry=self._secret_registry,
        )

    def _open(self, core: Path, vmlinux: Path) -> _Program:
        assert self._open_program is not None
        try:
            return self._open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env().get_artifact(ref, None).data


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = ["RemoteVmcoreIntrospect"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_introspect.py -q`
Expected: PASS (3 cases).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/introspect.py tests/providers/remote_libvirt/test_introspect.py
git commit -m "feat: add RemoteVmcoreIntrospect worker-side postmortem (ADR-0083)"
```

---

## Task 8: RemoteLiveIntrospect — in-guest drgn via the guest-agent seam

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/introspect.py` (append the class)
- Modify: `tests/providers/remote_libvirt/test_introspect.py` (append tests)

`introspect_live(transport_handle, helper)` validates `helper` worker-side against the fixed set,
treats `transport_handle` as the **guest domain name** (the pinned ADR-0083 §4 contract), opens
the qemu+tls connection from config, runs the allowlisted in-guest drgn helper via `GuestAgentExec`,
parses its JSON output, and assembles the redacted report. Off-gate / unknown helper fails closed.

- [ ] **Step 1: Write the failing tests** (append)

```python
# append to tests/providers/remote_libvirt/test_introspect.py
import base64
import json

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.guest_agent import AgentExecResult
from kdive.providers.remote_libvirt.introspect import RemoteLiveIntrospect
from tests.providers.remote_libvirt.conftest import RecordingBackend

_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


class _ScriptedAgent:
    """A qemu_agent_command double implementing the two-phase guest-exec protocol.

    Mirrors test_install.py's scripted agent so the tests exercise the **real** GuestAgentExec
    (and its worker-side allowlist), not a mock of it. `handler(argv)` returns the command's
    AgentExecResult or raises libvirt.libvirtError. Records every argv it ran.
    """

    def __init__(self, handler):
        self._handler = handler
        self._pending = {}
        self._next_pid = 1
        self.argvs: list[list[str]] = []

    def __call__(self, domain, command, timeout, flags):
        payload = json.loads(command)
        if payload["execute"] == "guest-exec":
            args = payload["arguments"]
            argv = [args["path"], *args["arg"]]
            result = self._handler(argv)
            self.argvs.append(argv)
            pid = self._next_pid
            self._next_pid += 1
            self._pending[pid] = result
            return json.dumps({"return": {"pid": pid}})
        if payload["execute"] == "guest-exec-status":
            result = self._pending.pop(payload["arguments"]["pid"])
            return json.dumps({"return": {
                "exited": True, "exitcode": result.exit_status,
                "out-data": base64.b64encode(result.stdout).decode(),
                "err-data": base64.b64encode(result.stderr).decode(),
            }})
        raise AssertionError(payload)


class _FakeDomain:
    def __init__(self, name): self._name = name
    def name(self): return self._name


class _FakeConn:
    def lookupByName(self, name): return _FakeDomain(name)  # noqa: N802 - libvirt binding name
    def close(self): pass


def _config_remote():
    return RemoteLibvirtConfig(uri="qemu+tls://h/system", cert_refs=_REFS,
                               concurrent_allocation_cap=1, gdb_addr="10.0.0.5")


def _live(agent):
    # RecordingBackend + a real GuestAgentExec run; only the libvirt opener is faked.
    return RemoteLiveIntrospect(
        secret_registry=SecretRegistry(),
        config_factory=_config_remote,
        open_connection=lambda _uri: _FakeConn(),
        agent_command=agent,
        secret_backend_factory=RecordingBackend,
    )


def test_introspect_live_unknown_helper_is_configuration_error():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"{}", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="evil")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.argvs == []  # rejected before any agent round-trip


def test_introspect_live_runs_allowlisted_helper_through_real_guest_agent():
    section = {"release": "6.1.0", "version": "v", "machine": "x86_64", "nodename": "n",
               "boot_cmdline": "ro", "cpus_online": 1, "mem_total_pages": 1}
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent)
    out = live.introspect_live(transport_handle="kdive-sys", helper="sysinfo")
    assert agent.argvs == [["/usr/local/sbin/kdive-drgn", "sysinfo"]]  # the single allowlisted program
    assert out.sysinfo["release"] == "6.1.0"


def test_introspect_live_nonzero_exit_is_debug_attach_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(1, b"", b"boom"))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="tasks")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_introspect_live_undecodable_output_is_infrastructure_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"not json", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="modules")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_introspect.py -q`
Expected: FAIL with `ImportError: cannot import name 'RemoteLiveIntrospect'`.

- [ ] **Step 3: Implement `RemoteLiveIntrospect`** (append to `introspect.py`; add imports at top)

Add these imports to the top of `introspect.py`:

```python
import json as _json
from typing import Any

import libvirt

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
```

Append:

```python
# The single allowlisted in-guest drgn helper the base image carries (ADR-0079); it runs the
# fixed in-tree helper named by argv[1] against /proc/kcore and prints that section as JSON.
_DRGN_HELPER = "/usr/local/sbin/kdive-drgn"
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})

type _OpenConnection = Callable[[str], Any]


class RemoteLiveIntrospect:
    """In-guest drgn-live over the qemu-guest-agent seam (ADR-0079/0083 §4).

    `transport_handle` carries the guest **domain name** (the pinned ADR-0083 §4 contract).
    The helper is validated worker-side against the fixed set before any agent round-trip, and
    the real `GuestAgentExec` enforces the single-program allowlist, so a guest-agent exec can
    never run an arbitrary program. The single redaction boundary is `assemble_report`. All
    slow/host seams (the agent round-trip, the libvirt opener, the secret backend) are injected,
    so unit tests drive the full two-phase protocol and the allowlist with no libvirt host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: _OpenConnection | None = None,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection if open_connection is not None else _open_libvirt
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLiveIntrospect:
        return cls(secret_registry=secret_registry)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        """Run one allowlisted in-guest drgn helper; return a redacted, byte-bounded report.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown helper or a blank handle;
                ``TRANSPORT_FAILURE`` for an unreachable guest agent; ``INFRASTRUCTURE_FAILURE``
                for a malformed agent reply or undecodable helper output; ``DEBUG_ATTACH_FAILURE``
                for a non-zero helper exit (drgn could not attach in-guest).
        """
        if helper not in _LIVE_HELPERS:
            raise CategorizedError(
                f"unknown live introspection helper: {helper}",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        domain_name = transport_handle.strip()
        if not domain_name:
            raise CategorizedError(
                "remote live introspection handle must carry a domain name",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        section = self._run_in_guest(domain_name, helper)
        tasks = section if helper == "tasks" else {}
        modules = section if helper == "modules" else {}
        sysinfo = section if helper == "sysinfo" else {}
        return assemble_report(
            tasks, modules, sysinfo,
            byte_cap=_REPORT_BYTE_CAP, secret_registry=self._secret_registry,
        )

    def _run_in_guest(self, domain_name: str, helper: str) -> dict[str, object]:
        result = self._exec(domain_name, [_DRGN_HELPER, helper])
        if result.exit_status != 0:
            raise CategorizedError(
                "in-guest drgn helper exited non-zero (could not attach to the live kernel)",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"domain": domain_name, "exit_status": result.exit_status},
            )
        try:
            decoded = _json.loads(result.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CategorizedError(
                "in-guest drgn helper returned undecodable JSON",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if not isinstance(decoded, dict):
            raise CategorizedError(
                "in-guest drgn helper output was not a JSON object",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return decoded

    def _exec(self, domain_name: str, argv: list[str]) -> AgentExecResult:
        """Open the qemu+tls connection, look up the domain, run argv via GuestAgentExec.

        Fully unit-testable with an injected `agent_command` + `open_connection` +
        `secret_backend_factory` (mirroring test_install.py); only `_open_libvirt`'s real
        `libvirt.open` is the `live_vm` seam.
        """
        agent = GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_DRGN_HELPER}),
        )
        config = self._config_factory()
        with remote_connection(
            config, self._secret_backend_factory(), open_connection=self._open_connection
        ) as conn:
            domain = conn.lookupByName(domain_name)
            return agent.run(domain, argv)


def _open_libvirt(uri: str) -> Any:  # pragma: no cover - live_vm
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]
```

Update the module `__all__` to `["RemoteLiveIntrospect", "RemoteVmcoreIntrospect"]`.

> **Note (ADR-0083 §4):** `transport_handle` is interpreted as a bare domain name, **not** a
> `TransportHandleData`. Do not call `TransportHandleData.decode` on it. The deferred MCP-routing
> follow-up will make `start_session` emit this domain-carrying handle.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_introspect.py -q`
Expected: PASS (6 cases total).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/remote_libvirt/introspect.py tests/providers/remote_libvirt/test_introspect.py
git commit -m "feat: add RemoteLiveIntrospect in-guest drgn via guest-agent (ADR-0083)"
```

---

## Task 9: Wire the remote runtime + remove the stubs

**Files:**
- Modify: `src/kdive/providers/composition.py:217-268` (`build_remote_runtime`)
- Modify: `src/kdive/providers/remote_libvirt/planes.py` (delete `UnimplementedConnector`, `UnimplementedIntrospector`)
- Modify: `tests/providers/remote_libvirt/test_planes.py` (drop the deleted-stub cases)
- Modify: `tests/providers/test_composition.py` (assert the remote runtime now has real ports)

- [ ] **Step 1: Write the failing composition test**

```python
# add to tests/providers/test_composition.py
def test_remote_runtime_wires_connect_and_introspect_ports():
    from kdive.providers.composition import build_remote_runtime
    from kdive.providers.remote_libvirt.connect import RemoteLibvirtConnect
    from kdive.providers.remote_libvirt.debug import remote_attach_seam
    from kdive.providers.remote_libvirt.introspect import (
        RemoteLiveIntrospect, RemoteVmcoreIntrospect,
    )
    from kdive.security.secrets.secret_registry import SecretRegistry

    runtime = build_remote_runtime(secret_registry=SecretRegistry())
    assert isinstance(runtime.connector, RemoteLibvirtConnect)
    assert runtime.attach_seam is remote_attach_seam
    assert isinstance(runtime.vmcore_introspector, RemoteVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, RemoteLiveIntrospect)
    # control/retrieve stay stubbed (issue #206)
    from kdive.providers.remote_libvirt.planes import UnimplementedController
    assert isinstance(runtime.controller, UnimplementedController)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/test_composition.py -q`
Expected: FAIL (remote runtime still wires `UnimplementedConnector`/`UnimplementedIntrospector`).

- [ ] **Step 3: Wire `build_remote_runtime`**

In `composition.py`, add imports:

```python
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.remote_libvirt.connect import RemoteLibvirtConnect
from kdive.providers.remote_libvirt.debug import remote_attach_seam
from kdive.providers.remote_libvirt.introspect import (
    RemoteLiveIntrospect,
    RemoteVmcoreIntrospect,
)
```

Remove `UnimplementedConnector` and `UnimplementedIntrospector` from the
`from ...planes import (...)` block (keep `UnimplementedController`, `UnimplementedRetriever`).

In `build_remote_runtime`, replace the stub bindings:

```python
    builder = RemoteLibvirtBuild.from_env(secret_registry=secret_registry)
    installer = RemoteLibvirtInstall.from_env(secret_registry=secret_registry)
    retriever = UnimplementedRetriever()
    vmcore_introspector = RemoteVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = RemoteLiveIntrospect.from_env(secret_registry=secret_registry)
    ...
    return ProviderRuntime(
        provisioner=RemoteLibvirtProvision(secret_registry=secret_registry),
        builder=builder,
        installer=installer,
        booter=installer,
        connector=RemoteLibvirtConnect.from_env(),
        controller=UnimplementedController(),
        retriever=retriever,
        crash_postmortem=retriever,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
        supported_capture_methods=frozenset(),
        discovery_registrar=register_remote_host,
        attach_seam=remote_attach_seam,
        debug_engine=GdbMiEngine(
            redactor_factory=lambda: Redactor(registry=secret_registry)
        ),
        component_sources=_remote_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=lambda _rootfs: None,
    )
```

Update the `build_remote_runtime` docstring to drop "fail-fast stubs for the connect/debug …
planes" and say the connect/debug + introspection planes are real (ADR-0083); control/retrieve
remain stubs (issue #206).

- [ ] **Step 4: Delete the now-unused stubs from `planes.py`**

Remove the `UnimplementedConnector` and `UnimplementedIntrospector` classes from
`remote_libvirt/planes.py`, their now-unused imports (`SystemHandle`, `TransportHandle`,
`IntrospectOutput`), and update the module docstring to list connect/debug + introspection as
real (only control/retrieve remain stubbed). In `tests/providers/remote_libvirt/test_planes.py`,
delete the test cases that asserted the two removed stubs raise `MISSING_DEPENDENCY`.

- [ ] **Step 5: Run the composition + planes + full remote suite**

Run: `uv run python -m pytest tests/providers/test_composition.py tests/providers/remote_libvirt -q`
Expected: PASS.

- [ ] **Step 6: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/composition.py src/kdive/providers/remote_libvirt/planes.py tests/providers/test_composition.py tests/providers/remote_libvirt/test_planes.py
git commit -m "feat: wire remote connect/debug/introspect ports; drop stubs (ADR-0083)"
```

---

## Task 10: Full guardrails + portability gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full local check suite**

Run: `just lint && just type && just test`
Expected: all green, zero warnings. Fix any import/type fallout from the moves before proceeding.

- [ ] **Step 2: Run the portability gate**

Run: `just m2-gate`
Expected: `gate passed: no core surface touched outside the ADR-0076 allowlist.` (All #205
changes are under `providers/` + `tests/providers/`, which is not a core prefix.)

- [ ] **Step 3: Run the doc + mermaid checks**

Run: `just check-mermaid && just docs-check`
Expected: both pass (no tool docstrings changed — M2 adds no tools).

- [ ] **Step 4: Commit any fixups**

```bash
git add -A
git commit -m "chore: guardrail fixups for remote connect/debug plane (#205)"
```

(Skip if the tree is clean.)

---

## Self-Review checklist (run before opening the PR)

- **Spec coverage:** ADR-0083 §1 (extraction) → Tasks 1–4. §2 (host policy) → Task 1 + Task 4 Step 2. §3 (gdbstub connector) → Task 5. §4 (in-guest drgn-live + pinned handle contract) → Task 8. §5 (worker-side vmcore) → Task 7. gdb-MI attach seam → Task 6. Runtime wiring + stub removal → Task 9. Gate + guardrails → Task 10. Acceptance criterion 1 (attach over direct TCP) → Tasks 5/6/9. Acceptance criterion 2 (in-guest drgn + vmcore) → Tasks 7/8 at the port level (MCP routing deferred per the ADR).
- **No core touches:** every modified path is under `src/kdive/providers/` or `tests/`. Confirm with `just m2-gate`.
- **Behavior-preserving moves:** Tasks 2–4 must leave local-libvirt's test counts unchanged; only import paths move. If a local test breaks on behavior (not import), stop — the move changed something it should not have.
- **Deferred work is filed:** at PR time, open the follow-up issue for (a) the drgn-live MCP routing (generalize `start_session`/`introspect.run` off the ssh model) and (b) the dead-worker gdbstub reconciler reset — both carry the gate-allowlist extension + ADR amendment.
