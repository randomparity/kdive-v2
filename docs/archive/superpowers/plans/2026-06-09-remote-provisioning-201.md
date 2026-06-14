# Remote Provisioning (M2 issue 2, #201) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `UnimplementedProvisioner` with `RemoteLibvirtProvision` — disk-image
base-OS profile, define/start over `qemu+tls://`, per-System storage-pool overlay, and a
per-System gdbstub port allocated + recorded in the domain XML (ADR-0080).

**Architecture:** A new `remote-libvirt` provider section + `BootMethod.DISK_IMAGE` in the
profile schema (`profiles/` — not gate-protected core); gdbstub/pool knobs added to the
operator env config and advertised by discovery; a new
`providers/remote_libvirt/provisioning.py` realizing the `Provisioner` port over the
ADR-0077 transport (made generic over the connection protocol); composition wires it in.
No core (`domain/db/jobs/reconciler/services/store/security/mcp`) file is touched — the
M2 portability gate must stay green.

**Tech Stack:** Python 3.13, pydantic profile models, `xml.etree.ElementTree` rendering /
`defusedxml` parsing, injected libvirt connection fakes (no real host in unit tests),
pytest.

**Verification at every commit:** `just lint && just type && just m2-gate` plus the
targeted test files; full `just test` before the final commit of each task that touches
shared surfaces (profiles, composition).

---

### Task 1: Profile schema — `RemoteLibvirtProfile` + `BootMethod.DISK_IMAGE`

**Files:**
- Modify: `src/kdive/profiles/provisioning.py`
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/profiles/test_provisioning.py`)

```python
_VALID_REMOTE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "remote-libvirt": {
            "base_image_volume": "kdive-base-fedora-42.qcow2",
            "crashkernel": "256M",
            "destructive_ops": ["force_crash"],
        }
    },
}


def _valid_remote() -> dict[str, Any]:
    return copy.deepcopy(_VALID_REMOTE)


def test_valid_remote_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())
    assert profile.boot_method is BootMethod.DISK_IMAGE
    section = profile.provider.remote_libvirt
    assert section.base_image_volume == "kdive-base-fedora-42.qcow2"


def test_remote_section_requires_disk_image_boot() -> None:
    data = _valid_remote()
    data["boot_method"] = "direct-kernel"
    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)
    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_disk_image_boot_requires_remote_section() -> None:
    data = _valid()  # local-libvirt section
    data["boot_method"] = "disk-image"
    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)
    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_profile_capture_method_kdump_with_crashkernel() -> None:
    assert capture_method(ProvisioningProfile.parse(_valid_remote())) is CaptureMethod.KDUMP


def test_remote_profile_capture_method_gdbstub_without_crashkernel() -> None:
    data = _valid_remote()
    del data["provider"]["remote-libvirt"]["crashkernel"]
    assert capture_method(ProvisioningProfile.parse(data)) is CaptureMethod.GDBSTUB


def test_remote_profile_destructive_opt_in() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())
    assert destructive_opt_in(profile, DestructiveJobKind.FORCE_CRASH) is True


def test_remote_profile_rootfs_and_ssh_are_none() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())
    assert rootfs_source(profile) is None
    assert ssh_credential_ref(profile) is None


def test_remote_profile_rejects_unknown_fields() -> None:
    data = _valid_remote()
    data["provider"]["remote-libvirt"]["bogus"] = "x"
    with pytest.raises(CategorizedError):
        ProvisioningProfile.parse(data)
```

(Import `rootfs_source` and `DestructiveJobKind` at the top of the test module; check
which are already imported.)

- [ ] **Step 2: Run, verify failure** — `uv run python -m pytest tests/profiles/test_provisioning.py -q` fails on the remote-section parse.

- [ ] **Step 3: Implement** in `src/kdive/profiles/provisioning.py`:

```python
class BootMethod(StrEnum):
    DIRECT_KERNEL = "direct-kernel"
    DISK_IMAGE = "disk-image"


class RemoteLibvirtProfile(_ProfileBase):
    """The ``remote-libvirt`` provider section (ADR-0080)."""

    base_image_volume: NonEmptyStr
    crashkernel: NonEmptyStr | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
```

- `ProviderSection`: add `remote_libvirt_section` (aliases `ResourceKind.REMOTE_LIBVIRT.value`),
  extend `_require_exactly_one_provider`'s `present` list, add a `remote_libvirt` property
  mirroring the others.
- `ProvisioningProfile`: add a `model_validator(mode="after")` pairing boot method and section:

```python
@model_validator(mode="after")
def _pair_boot_method_with_provider(self) -> ProvisioningProfile:
    remote = self.provider.remote_libvirt_section is not None
    disk_image = self.boot_method is BootMethod.DISK_IMAGE
    if remote != disk_image:
        raise ValueError(
            "boot_method 'disk-image' and the remote-libvirt provider section "
            "require each other (ADR-0080)"
        )
    return self
```

- `capture_method`: before the fault-inject fallback, handle the remote section
  (crashkernel → `KDUMP`, else `GDBSTUB`).
- `destructive_opt_in`: handle the remote section's `destructive_ops`.
  (`rootfs_source` / `ssh_credential_ref` read the local section only — already `None`.)

- [ ] **Step 4: Run, verify pass** — same command, all green.
- [ ] **Step 5:** `just lint && just type && uv run python -m pytest tests/profiles -q`, commit `feat: add remote-libvirt provisioning profile section (ADR-0080)`.

---

### Task 2: Operator config — pool + gdbstub knobs; discovery advertises them

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/config.py`
- Modify: `src/kdive/providers/remote_libvirt/discovery.py`
- Test: `tests/providers/remote_libvirt/test_config.py`, `tests/providers/remote_libvirt/test_discovery.py`

- [ ] **Step 1: Failing tests** — config: defaults (`storage_pool="default"`, `gdb_addr is None`,
  ports `47000`/`47099`), explicit values, non-int port → `CONFIGURATION_ERROR`,
  `min > max` → `CONFIGURATION_ERROR`, port outside 1–65535 → `CONFIGURATION_ERROR`.
  Discovery: capabilities include `storage_pool`, `gdbstub_port_min`, `gdbstub_port_max`
  always, `gdbstub_addr` only when configured.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** — `RemoteLibvirtConfig` gains:

```python
storage_pool: str
gdb_addr: str | None
gdb_port_min: int
gdb_port_max: int
```

Env names: `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`, `_GDB_ADDR`, `_GDB_PORT_MIN`, `_GDB_PORT_MAX`
(defaults `default`, unset, `47000`, `47099`). A `_port_env(name, default)` helper maps
non-int / out-of-range to `CONFIGURATION_ERROR`; `min > max` is a `CONFIGURATION_ERROR`.
Discovery's `capabilities` dict adds the three always-present keys and `gdbstub_addr` when set.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5:** Guardrails, commit `feat: add storage-pool + gdbstub operator config (ADR-0080)`.

---

### Task 3: Generic `remote_connection` (transport reuse without protocol widening)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/transport.py`
- Test: `tests/providers/remote_libvirt/test_transport.py` (existing suite must stay green)

- [ ] **Step 1:** Make `remote_connection` generic so provisioning can inject a wider
  connection protocol while discovery keeps its narrow one:

```python
class ClosableConn(Protocol):
    def close(self) -> None: ...


@contextmanager
def remote_connection[C: ClosableConn](
    config: RemoteLibvirtConfig,
    secret_backend: SecretBackend,
    *,
    open_connection: Callable[[str], C],
    pki_base_dir: Path | None = None,
) -> Iterator[C]: ...
```

Body unchanged. `OpenConnection` alias and `_LibvirtConn` stay for discovery.
- [ ] **Step 2:** `uv run python -m pytest tests/providers/remote_libvirt -q && just type` — existing suite green, no behavior change to assert.
- [ ] **Step 3:** Commit `refactor: make remote_connection generic over the connection protocol`.

---

### Task 4: Provisioning module — rendering + port allocation (pure parts)

**Files:**
- Create: `src/kdive/providers/remote_libvirt/provisioning.py`
- Test: `tests/providers/remote_libvirt/test_provisioning.py` (create)

- [ ] **Step 1: Failing tests** for the pure functions:

```python
def test_render_domain_xml_carries_agent_channel_gdb_and_metadata() -> None:
    xml = render_domain_xml(
        SYSTEM_ID, _remote_profile(), pool="kdive", volume="kdive-...-overlay.qcow2",
        gdb_addr="10.0.0.5", gdb_port=47001,
    )
    root = fromstring(xml)  # defusedxml in tests is fine
    channel = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    assert channel is not None
    args = [a.get("value") for a in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")]
    assert args == ["-gdb", "tcp:10.0.0.5:47001"]
    assert root.findtext(f"./metadata/{{{KDIVE_NS}}}system") == str(SYSTEM_ID)
    assert root.find("./os/boot[@dev='hd']") is not None
    disk_source = root.find("./devices/disk/source")
    assert disk_source.get("pool") == "kdive"
    assert root.findtext("./uuid") == str(SYSTEM_ID)
    assert root.find("./devices/serial/log") is None  # no worker-local tee (ADR-0080)


def test_recorded_gdb_port_roundtrip_and_tolerance() -> None:
    xml = render_domain_xml(...)
    assert recorded_gdb_port(xml) == 47001
    assert recorded_gdb_port("<domain><name>x</name></domain>") is None
    assert recorded_gdb_port("not xml") is None


def test_allocate_lowest_free_port_skips_used_and_reuses_own() -> None:
    used = {"kdive-a": 47000, "kdive-b": 47002}
    assert allocate_gdb_port(used, own_name="kdive-new", port_min=47000, port_max=47005) == 47001
    assert allocate_gdb_port(used, own_name="kdive-a", port_min=47000, port_max=47005) == 47000


def test_allocate_exhausted_range_raises_provisioning_failure() -> None:
    used = {"kdive-a": 47000, "kdive-b": 47001}
    with pytest.raises(CategorizedError) as exc_info:
        allocate_gdb_port(used, own_name="kdive-new", port_min=47000, port_max=47001)
    assert exc_info.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_render_volume_xml_backing_store() -> None:
    xml = render_volume_xml("kdive-X-overlay.qcow2", capacity_bytes=42, backing_path="/p/base.qcow2")
    root = fromstring(xml)
    assert root.findtext("./name") == "kdive-X-overlay.qcow2"
    assert root.findtext("./capacity") == "42"
    assert root.findtext("./backingStore/path") == "/p/base.qcow2"
    assert root.find("./backingStore/format").get("type") == "qcow2"
    assert root.find("./target/format").get("type") == "qcow2"
```

- [ ] **Step 2: Run, verify failure** (`ImportError`).
- [ ] **Step 3: Implement** the module skeleton: namespace constants
  (`_KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"` — duplicated from local-libvirt
  deliberately, ADR-0076; `_QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"`),
  ElementTree namespace registration at the rendering boundary, `overlay_volume_name()`,
  `render_volume_xml()`, `render_domain_xml()` (shell, disk `type='volume'`, virtio-serial
  guest-agent channel, pty serial/console without `<log>`, qemu:commandline gdb args,
  metadata tag, `require_concrete_sizing` + remote-section check), `recorded_gdb_port()`
  (defusedxml parse, tolerant of absent/malformed), `allocate_gdb_port()`.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5:** Guardrails, commit `feat: remote domain/volume XML rendering + gdb port allocation`.

---

### Task 5: `RemoteLibvirtProvision.provision` (orchestration over fakes)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/provisioning.py`
- Test: `tests/providers/remote_libvirt/test_provisioning.py`

Fakes: `FakeVolume` (path/info/delete), `FakePool` (volumes dict; `storageVolLookupByName`
raising a fake `libvirtError(VIR_ERR_NO_STORAGE_VOL)` when absent; `createXML` records),
`FakeDomain` (xml, active flag, create/destroy/undefine recording; `XMLDesc` returns
stored XML — with the agent channel `state` attribute injectable), `FakeProvisionConn`
(domains dict, `defineXML` parses name from XML and stores, `listAllDomains`,
`lookupByName`, `storagePoolLookupByName`, close). Error doubles use the **verified**
pattern from `tests/providers/local_libvirt/fakes.py:libvirt_error(code)` — build
`libvirt.libvirtError("synthetic")` and set `err.err = (code, 0, "synthetic", 0, "",
None, None, 0, 0)` so `get_error_code()` returns the code. Duplicate that one helper
into `tests/providers/remote_libvirt/conftest.py` (no shared layer with local-libvirt,
ADR-0076 — the same deliberate duplication the source rule forces).

Config/env: build `RemoteLibvirtConfig` directly in tests (no env), pass
`config_factory=lambda: config`. Inject `sleep=lambda s: None` and a controllable
`monotonic` (list-driven) for the agent wait; `agent poll` returns `connected` after N polls.

- [ ] **Step 1: Failing tests:**
  - happy path: returns `kdive-<id>`; domain defined+created; overlay created with
    backing = base path; gdb port 47000 recorded in defined XML; agent polled to connected.
  - overlay reused when present (no `createXML` call).
  - port allocation skips ports recorded by other defined kdive domains.
  - provision retry on own existing domain reuses its recorded port.
  - `create()` raising `VIR_ERR_OPERATION_INVALID` (already running) → success.
  - start failure advances port: first `create()` raises (bind), second succeeds on next
    port; first define undefined.
  - start failure exhausting the bounded attempts → `PROVISIONING_FAILURE`, domain
    undefined, overlay deleted **only if created this attempt**.
  - missing base volume → `CONFIGURATION_ERROR`.
  - missing storage pool → `CONFIGURATION_ERROR`.
  - overlay `createXML` failure → `PROVISIONING_FAILURE` (no domain defined).
  - a domain vanishing between `listAllDomains` and `XMLDesc`
    (`VIR_ERR_NO_DOMAIN`) is skipped during port enumeration; provision succeeds.
  - profile without remote section → `CONFIGURATION_ERROR` (no connection opened).
  - `gdb_addr` unset → `CONFIGURATION_ERROR` (no connection opened).
  - agent never connects → `PROVISIONING_FAILURE`, domain NOT undefined (left running).
  - domain exits during agent wait (active→False) → fast `PROVISIONING_FAILURE`
    mentioning exit, without exhausting the timeout.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** `RemoteLibvirtProvision.__init__` (keyword-only:
  `secret_registry`, `config_factory=remote_config_from_env`, `open_connection=open_libvirt`,
  `pki_base_dir=None`, `sleep=time.sleep`, `monotonic=time.monotonic`,
  `agent_timeout_s=180.0`, `agent_poll_s=2.0`) + `provision()` per ADR-0080 §2–4:
  validate section + config → connect → pool/base lookup → overlay ensure → enumerate
  used ports (skip vanished domains via `VIR_ERR_NO_DOMAIN`) → bounded define/start loop
  with port advance (3 attempts) → agent wait (fail fast on inactive) → return name.
  Error codes per the port contract + ADR-0080 (`CONFIGURATION_ERROR` /
  `PROVISIONING_FAILURE` / `INFRASTRUCTURE_FAILURE`; `TRANSPORT_FAILURE` propagates from
  `remote_connection`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5:** Guardrails, commit `feat: RemoteLibvirtProvision define/start over TLS`.

---

### Task 6: Teardown + reprovision

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/provisioning.py`
- Test: `tests/providers/remote_libvirt/test_provisioning.py`

- [ ] **Step 1: Failing tests:**
  - teardown destroys + undefines + deletes the overlay volume.
  - teardown of an absent domain still deletes the overlay (idempotent re-teardown).
  - teardown with absent overlay volume is a no-op success.
  - teardown reads the pool from the live domain XML when it differs from config
    (pool-drift case, ADR-0080) and deletes the overlay from the recorded pool.
  - destroy on a not-running domain (`VIR_ERR_OPERATION_INVALID`) is swallowed.
  - other libvirt errors → `INFRASTRUCTURE_FAILURE`.
  - reprovision = teardown then provision (records call order; new domain defined).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** `teardown()` (pool-from-domain-XML with config fallback;
  swallow `VIR_ERR_NO_DOMAIN` / `VIR_ERR_OPERATION_INVALID` / `VIR_ERR_NO_STORAGE_VOL` /
  `VIR_ERR_NO_STORAGE_POOL` as achieved post-states) and `reprovision()`.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5:** Guardrails, commit `feat: remote teardown + reprovision with overlay reclaim`.

---

### Task 7: Composition wiring + remove the dead stub

**Files:**
- Modify: `src/kdive/providers/composition.py` (provisioner = `RemoteLibvirtProvision(secret_registry=...)`)
- Modify: `src/kdive/providers/remote_libvirt/planes.py` (delete `UnimplementedProvisioner` — replace, don't deprecate)
- Test: `tests/providers/test_composition.py`, `tests/providers/remote_libvirt/test_planes.py`

- [ ] **Step 1: Failing test** — `build_remote_runtime(...).provisioner` is a
  `RemoteLibvirtProvision`; the runtime stays **constructible with no
  `KDIVE_REMOTE_LIBVIRT_*` env set** (the ADR-0076 buildability invariant — provision
  reads config at op time, never at construction); update/remove the planes test for
  the deleted stub.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** the wiring; delete the stub class and its export/usages.
- [ ] **Step 4: Run, verify pass** — `uv run python -m pytest tests/providers -q`.
- [ ] **Step 5:** Guardrails incl. `just m2-gate`, commit `feat: wire RemoteLibvirtProvision into the remote runtime`.

---

### Task 8: Port-contract docstrings + full sweep

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py` (Provisioner docstrings gain
  `TRANSPORT_FAILURE` for remote control-channel connect faults — documentation only,
  no signature change)

- [ ] **Step 1:** Apply the docstring deltas.
- [ ] **Step 2:** Full local suite: `just ci` (lint, type, lock-check, shell/workflow lint,
  mermaid, docs-check, m2-gate, test). `docs-check` may regenerate tool reference docs —
  M2 adds no tools, so expect no drift; if it regenerates, commit the regeneration.
- [ ] **Step 3:** Commit `docs: document transport_failure on the Provisioner contract`.

---

## Self-review notes

- **Spec coverage:** profile (Task 1) ↔ ADR §1; config (Task 2) ↔ §5; port registry +
  rendering (Task 4) ↔ §2; provision semantics incl. agent gate + port advance (Task 5)
  ↔ §2/§4; overlay (Tasks 4–6) ↔ §3; teardown/reprovision + pool drift (Task 6) ↔ §4;
  wiring/opt-in (Task 7) ↔ §5. Acceptance "agent responds" is the Task-5 agent-wait
  tests; real-host proof is issue 8 (out of scope here, by spec).
- **Rollback:** every task is an independent commit; the branch merges atomically via PR.
- **Gate:** no task touches a core prefix; Task 8's `just m2-gate` re-proves it.
