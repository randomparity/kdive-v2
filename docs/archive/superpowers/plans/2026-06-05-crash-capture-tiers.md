# Crash-capture tiers — Implementation Plan (Phase 0 + groundwork + Tier 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De-risk the four host-behavior unknowns, stand up the local hardware spine, and ship Tier 0 (console capture) end-to-end so an agent can boot a kernel with `dhash_entries=1` and read the crash console.

**Architecture:** Follows the spec `docs/superpowers/specs/2026-06-05-crash-capture-tiers-design.md` and ADR-0049. The capture-method vocabulary is a domain-level enum dispatched per plane; this plan builds the shared groundwork (enum, profile `debug` block, `vmcore.fetch` method validation) and the console tier (always-on serial `<log file>`, registered as an `artifacts.*` object by the boot handler). Host-touching seams stay injected/faked in unit tests (the codebase's established `live_vm`-gate pattern); a single `test-live` gated test exercises the real host.

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`; libvirt/QEMU via `qemu:///system`; Pydantic profiles; Postgres job/artifact ledger.

**Scope note:** Tier 1 (`host_dump`) and Tier 2 (`gdbstub`) are **deferred to follow-on plans** gated on Phase 0 findings — their task code depends on empirical answers to §13.2/§13.3/§13.4. This plan produces working, demoable software on its own (boot → read crash console → clean-boot baseline for A/B).

**Commands:** `just lint` · `just type` · `just test` (CI runs these individually). Live tests: `just test-live`. Single test: `uv run pytest <path>::<name> -q`.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `scripts/live-vm/build-busybox-initramfs.sh` | Minimal Tier-0 boot vehicle (initramfs that triggers a path lookup) | Create (Phase 0) |
| `~/src/linux/.config` | Debug kernel config (KASAN, CRASH_DUMP, DWARF5, pvpanic, fw_cfg vmcoreinfo, KASLR off) | Create (Phase 0) |
| `docs/runbooks/crash-capture-spine.md` | Phase 0 de-risk findings (resolves §13.1–4) | Create (Phase 0) |
| `src/kdive/domain/capture.py` | `CaptureMethod` vocabulary enum + local-libvirt supported-set | Create |
| `src/kdive/profiles/provisioning.py` | `LibvirtDebugOptions`, `debug` field, optional `crashkernel` | Modify |
| `src/kdive/providers/local_libvirt/provisioning.py` | `render_domain_xml`: always-on console `<serial>`+`<log>` | Modify |
| `src/kdive/providers/local_libvirt/install.py` | `_kdump_check` method-conditional; optional initrd; `read_console_log` seam | Modify |
| `src/kdive/mcp/tools/vmcore.py` | `vmcore.fetch` `method` arg, supported-set validation, payload/dedup | Modify |
| `src/kdive/providers/local_libvirt/retrieve.py` | `capture(system_id, method)` signature | Modify |
| `src/kdive/mcp/tools/runs.py` | `boot_handler`: register the console artifact on boot-window close | Modify |
| Tests mirror each under `tests/…` | | Create/Modify |

---

## Phase 0 — Host setup & de-risk (no feature code; gates everything)

### Task 0.0: Build the minimal boot vehicle (busybox initramfs + placeholder rootfs)

Tier 0 only needs to *boot a custom kernel far enough to do a path lookup* and observe the
console. A busybox initramfs is the standard, reliable vehicle for that — it avoids the
custom-kernel-vs-cloud-image module/initramfs mismatch (the Fedora Cloud catalog image
`fedora-cloud-base-43-x86_64` is for later tiers that need real userspace/SSH, not Tier 0). The
provisioning model still requires a `rootfs` disk, so we also create a tiny placeholder qcow2;
Tier 0 boots the initramfs, not the disk.

**Files:**
- Create: `scripts/live-vm/build-busybox-initramfs.sh` (checked in)

- [ ] **Step 1: Write the initramfs builder**

Create `scripts/live-vm/build-busybox-initramfs.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
# Build a minimal busybox initramfs whose init triggers a dcache path lookup, prints a
# readiness marker to the console, then idles. Output: $1 (a cpio, for CONFIG_INITRAMFS_SOURCE).
OUT="${1:?usage: build-busybox-initramfs.sh <out.cpio>}"
BB="$(command -v busybox)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work"/{bin,proc,sys,dev}
cp "$BB" "$work/bin/busybox"
ln -s busybox "$work/bin/sh"
# Fedora's busybox is dynamically linked: bundle its loader + shared libraries, or /init
# fails to exec (missing ld-linux/libc) and the VM never reaches the path lookup.
ldd "$BB" | grep -oE '/[^ ]+\.so[^ ]*' | while read -r so; do
  install -D "$so" "$work$so"
done
cat >"$work/init" <<'INIT'
#!/bin/sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sys /sys
/bin/busybox ls /proc >/dev/null    # path lookup; crashes a vulnerable kernel here
echo "KDIVE-BUSYBOX-READY"           # the Tier-0 clean-boot readiness marker
exec /bin/busybox sh
INIT
chmod +x "$work/init"
( cd "$work" && find . | cpio -o -H newc ) >"$OUT"
echo "wrote $OUT"
```

- [ ] **Step 2: Lint the script and build the initramfs**

Run:
```bash
shellcheck scripts/live-vm/build-busybox-initramfs.sh && shfmt -d scripts/live-vm/build-busybox-initramfs.sh
chmod +x scripts/live-vm/build-busybox-initramfs.sh
sudo dnf install -y busybox cpio  # if absent (host-privileged; run via `! sudo …`)
scripts/live-vm/build-busybox-initramfs.sh /tmp/kdive-initramfs.cpio
ls -l /tmp/kdive-initramfs.cpio
```
Expected: the `.cpio` exists and shellcheck/shfmt are clean.

- [ ] **Step 3: Create the placeholder rootfs disk (satisfies the required `rootfs` field)**

Run:
```bash
qemu-img create -f qcow2 /var/lib/libvirt/images/kdive-tier0-rootfs.qcow2 1G
```
Expected: the qcow2 exists in `/var/lib/libvirt/images`. (Tier 0 boots the initramfs embedded in the kernel; this disk is unused but the System profile requires a `rootfs: {kind: path, path: …}` reference.) The pool created in Task 0.2 picks it up on `pool-build`/`pool-start`; if the pool already exists, `virsh -c qemu:///system pool-refresh kdive`.

- [ ] **Step 4: Commit the builder**

```bash
git add scripts/live-vm/build-busybox-initramfs.sh
git commit -m "chore(live-vm): add busybox initramfs builder for the Tier-0 boot vehicle"
```

### Task 0.1: Generate the debug kernel `.config`

**Files:**
- Create: `~/src/linux/.config` (via a tracked fragment)
- Create: `scripts/live-vm/x86_64-debug.config` (the merge fragment, checked in)

- [ ] **Step 1: Write the config fragment**

Create `scripts/live-vm/x86_64-debug.config`:

```
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y
CONFIG_KEXEC=y
CONFIG_CRASH_DUMP=y
CONFIG_DEBUG_INFO_DWARF5=y
# CONFIG_RANDOMIZE_BASE is not set
CONFIG_PVPANIC=y
CONFIG_PVPANIC_PCI=y
CONFIG_FW_CFG_SYSFS=y
CONFIG_SERIAL_8250=y
CONFIG_SERIAL_8250_CONSOLE=y
CONFIG_VIRTIO=y
CONFIG_VIRTIO_PCI=y
CONFIG_VIRTIO_BLK=y
CONFIG_VIRTIO_CONSOLE=y
```

- [ ] **Step 2: Merge into a defconfig and normalize**

Run:
```bash
cd ~/src/linux
make x86_64_defconfig
./scripts/kconfig/merge_config.sh -m .config /home/dave/src/kdive/scripts/live-vm/x86_64-debug.config
make olddefconfig
```

- [ ] **Step 3: Verify the load-bearing symbols are set**

Run:
```bash
cd ~/src/linux
grep -E 'CONFIG_(KASAN|CRASH_DUMP|PVPANIC|FW_CFG_SYSFS|DEBUG_INFO_DWARF5)=y' .config
grep -E 'CONFIG_RANDOMIZE_BASE' .config
```
Expected: the first five print `=y`; `RANDOMIZE_BASE` prints `# CONFIG_RANDOMIZE_BASE is not set`.

- [ ] **Step 4: Commit the fragment**

```bash
cd /home/dave/src/kdive
git add scripts/live-vm/x86_64-debug.config
git commit -m "chore(live-vm): add x86_64 debug kernel config fragment"
```
(The generated `~/src/linux/.config` is outside this repo and is not committed.)

### Task 0.2: Define and start the `qemu:///system` storage pool

- [ ] **Step 1: Define + build + start a dir pool**

Run:
```bash
virsh -c qemu:///system pool-define-as kdive dir --target /var/lib/libvirt/images
virsh -c qemu:///system pool-build kdive
virsh -c qemu:///system pool-start kdive
virsh -c qemu:///system pool-autostart kdive
```

- [ ] **Step 2: Verify**

Run: `virsh -c qemu:///system pool-list --all`
Expected: `kdive` listed, State `active`, Autostart `yes`.

### Task 0.3: Manual spine-up — resolve §13.1–§13.4 empirically

**Files:**
- Create: `docs/runbooks/crash-capture-spine.md` (record findings + the exact XML/cmdline used)

- [ ] **Step 1: Build the kernel**

Run:
```bash
cd ~/src/linux
# Embed the Task-0.0 initramfs so the single bzImage is self-contained — no external initrd,
# which sidesteps the install plane fetching kernel+initrd from one ref (install.py:153-154).
./scripts/config --set-str CONFIG_INITRAMFS_SOURCE /tmp/kdive-initramfs.cpio
make olddefconfig
make -j"$(nproc)" bzImage
ls -l arch/x86_64/boot/bzImage vmlinux
```
Expected: both exist; `grep CONFIG_INITRAMFS_SOURCE .config` shows the cpio path.

- [ ] **Step 2: Write the probe domain XML**

Create `/tmp/kdive-probe.xml` (direct-kernel boot of the Task-0.0 busybox initramfs; the disk is
present only to satisfy the device model — the initramfs is the boot target):
```xml
<domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
  <name>kdive-probe</name>
  <memory unit='MiB'>2048</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <kernel>/home/dave/src/linux/arch/x86_64/boot/bzImage</kernel>
    <cmdline>console=ttyS0 dhash_entries=1 panic_on_oops=1 kasan.fault=panic panic=0 nokaslr</cmdline>
  </os>
  <on_crash>preserve</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/kdive-tier0-rootfs.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <serial type='pty'><log file='/tmp/kdive-probe-console.log'/><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <panic model='pvpanic'/>
  </devices>
  <qemu:commandline>
    <qemu:arg value='-gdb'/>
    <qemu:arg value='tcp:127.0.0.1:55555,server=on,wait=off'/>
  </qemu:commandline>
</domain>
```
Validate before use: `virt-xml-validate /tmp/kdive-probe.xml` (expected: `/tmp/kdive-probe.xml validates`).

- [ ] **Step 3: Boot and observe (resolves §13.1 — does it panic→crashed)**

Run:
```bash
virsh -c qemu:///system create /tmp/kdive-probe.xml
sleep 20
virsh -c qemu:///system domstate kdive-probe
grep -iE 'd_lookup|KASAN|Kernel panic|BUG' /tmp/kdive-probe-console.log | head
```
Record: did `domstate` report `crashed`? Did the console name `__d_lookup()`? (§13.1)

- [ ] **Step 4: Dump the frozen domain (resolves §13.4 — virsh dump on crashed-state)**

Run:
```bash
virsh -c qemu:///system dump --memory-only kdive-probe /tmp/kdive-probe.vmcore
echo "exit=$?"; ls -l /tmp/kdive-probe.vmcore; head -c4 /tmp/kdive-probe.vmcore | xxd
```
Record: exit status 0? ELF magic `7f454c46`? If it rejects a crashed-state domain, note that Tier 1 must switch to pause-on-panic (§13.4 contingency).

- [ ] **Step 5: Check the dump for the build-id note (resolves §13.3 — vmcoreinfo)**

Run:
```bash
readelf -n /tmp/kdive-probe.vmcore 2>/dev/null | grep -iA2 'VMCOREINFO\|build' | head
drgn -c /tmp/kdive-probe.vmcore -e 'print(prog["UTS_RELEASE"])' 2>&1 | head
```
Record: is `VMCOREINFO` present with a build-id? (§13.3) If absent, Tier 1 needs a host_dump-specific provenance fallback.

- [ ] **Step 6: Attach the gdbstub (smoke for Tier 2)**

Run: `gdb -ex 'target remote 127.0.0.1:55555' -ex 'bt' -ex 'detach' -ex 'quit' ~/src/linux/vmlinux`
Record: did it attach and backtrace?

- [ ] **Step 7: Clean up and record findings**

Run: `virsh -c qemu:///system destroy kdive-probe`
Write `docs/runbooks/crash-capture-spine.md` with a table: §13.1/§13.3/§13.4 → confirmed / adjusted (+ the exact mechanism that worked). Commit:
```bash
git add docs/runbooks/crash-capture-spine.md && git commit -m "docs(runbook): crash-capture spine de-risk findings"
```

> **Gate:** Phase 0 findings finalize the Tier-1/Tier-2 plans. If §13.4 forced pause-on-panic, or §13.3 found no build-id note, note it — those follow-on plans branch on it. Phase 1 and Phase 2 below do **not** depend on these answers.

---

## Phase 1 — Shared groundwork

### Task 1.1: The `CaptureMethod` vocabulary + supported-set

**Files:**
- Create: `src/kdive/domain/capture.py`
- Test: `tests/domain/test_capture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/domain/test_capture.py`:
```python
"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod


def test_vocabulary_has_four_methods() -> None:
    assert {m.value for m in CaptureMethod} == {"console", "host_dump", "gdbstub", "kdump"}


def test_local_libvirt_supports_three_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert LOCAL_LIBVIRT_SUPPORTED == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
    assert CaptureMethod.KDUMP not in LOCAL_LIBVIRT_SUPPORTED
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/domain/test_capture.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.domain.capture`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/kdive/domain/capture.py`:
```python
"""The provider-agnostic crash-capture method vocabulary (ADR-0049 Decision 1)."""

from __future__ import annotations

from enum import StrEnum


class CaptureMethod(StrEnum):
    """A capture verb; each provider maps it to a mechanism (or rejects it)."""

    CONSOLE = "console"
    HOST_DUMP = "host_dump"
    GDBSTUB = "gdbstub"
    KDUMP = "kdump"


LOCAL_LIBVIRT_SUPPORTED: frozenset[CaptureMethod] = frozenset(
    {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
)
"""The methods local-libvirt realizes today; `kdump` joins via #115."""
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/domain/test_capture.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/domain/capture.py tests/domain/test_capture.py
git commit -m "feat(domain): add CaptureMethod vocabulary + local-libvirt supported-set"
```

### Task 1.2: Profile `debug` block + optional `crashkernel`

**Files:**
- Modify: `src/kdive/profiles/provisioning.py:82-104` (`LibvirtProfile`)
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/profiles/test_provisioning.py`:
```python
def test_debug_block_defaults_to_disabled() -> None:
    profile = ProvisioningProfile.parse(_valid())
    debug = profile.provider.local_libvirt.debug
    assert debug.preserve_on_crash is False
    assert debug.gdbstub is False


def test_debug_flags_parse_when_present() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"preserve_on_crash": True, "gdbstub": True}
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.debug.preserve_on_crash is True
    assert profile.provider.local_libvirt.debug.gdbstub is True


def test_debug_block_rejects_unknown_key() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"bogus": True}
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_crashkernel_is_optional() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["crashkernel"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.crashkernel is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/profiles/test_provisioning.py -q -k "debug or crashkernel_is_optional"`
Expected: FAIL — `debug` attribute missing / `crashkernel` required.

- [ ] **Step 3: Implement the schema change**

In `src/kdive/profiles/provisioning.py`, add above `LibvirtProfile`:
```python
class LibvirtDebugOptions(_ProfileBase):
    """Per-System debug provisioning flags (ADR-0049 Decision 3).

    Bound at provision/boot; declare which capture methods the System is
    provisioned for. ``preserve_on_crash`` adds a pvpanic device +
    ``<on_crash>preserve>``; ``gdbstub`` adds the QEMU ``-gdb`` argument.
    """

    preserve_on_crash: bool = False
    gdbstub: bool = False
```
Then in `LibvirtProfile`, change `crashkernel` and add `debug`:
```python
    crashkernel: NonEmptyStr | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    ssh_credential_ref: NonEmptyStr | None = None
    debug: LibvirtDebugOptions = Field(default_factory=LibvirtDebugOptions)
```

- [ ] **Step 4: Run to verify they pass (and no regression)**

Run: `uv run pytest tests/profiles/test_provisioning.py -q`
Expected: PASS (all, including the pre-existing `test_crashkernel_is_present`, which still supplies `crashkernel`).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py
git commit -m "feat(profiles): add libvirt debug flags; make crashkernel optional"
```

### Task 1.3: `vmcore.fetch` gains `method` with supported-set validation

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve.py:157` (`capture` signature)
- Modify: `src/kdive/mcp/tools/vmcore.py:127-149,202-215,337-341`
- Test: `tests/mcp/test_vmcore_tools.py` (exists), `tests/providers/local_libvirt/test_retrieve.py` (exists)

- [ ] **Step 1: Write the failing test for the capture signature**

`tests/providers/local_libvirt/test_retrieve.py` already has a `_FakeStore` (≈ line 52, returns a real `StoredArtifact`) and a `_SYS` constant — reuse them; do **not** redefine `_FakeStore`. Add `from kdive.domain.capture import CaptureMethod` and append:
```python
def test_capture_host_dump_uses_dump_seam() -> None:
    store = _FakeStore()
    retr = LocalLibvirtRetrieve(
        tenant="local",
        store_factory=lambda: store,
        wait_for_vmcore=lambda _sid: pytest.fail("kdump seam used for host_dump"),
        read_vmcore_build_id=lambda _b: "bid",
        extract_redacted=lambda _b: b"dmesg",
        host_dump_capture=lambda _sid: b"\x7fELFcore",
    )
    out = retr.capture(_SYS, CaptureMethod.HOST_DUMP)
    assert out.vmcore_build_id == "bid"
    assert out.raw is not None and out.redacted is not None
```

**Regression:** the existing `test_capture_stores_two_artifacts_and_returns_build_id`,
`test_capture_no_core_is_readiness_failure`, and `test_capture_store_failure_is_infrastructure_failure`
call `capture(_SYS)` with one arg; the new required `method` breaks them — update those three to
`capture(_SYS, CaptureMethod.KDUMP)` (they exercise the `wait_for_vmcore`/kdump path) in the same commit.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_retrieve.py::test_capture_host_dump_uses_dump_seam -q`
Expected: FAIL — `capture()` takes 1 positional arg / no `host_dump_capture` param.

- [ ] **Step 3: Implement method-dispatch in `capture`**

In `retrieve.py`, add a `host_dump_capture` seam to `__init__`/`from_env` (defaulting to a `_real_host_dump_capture` stub raising `MISSING_DEPENDENCY`, mirroring the other live seams), and change `capture`:
```python
    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a core via ``method``; store raw + redacted; return refs + build-id."""
        if method is CaptureMethod.HOST_DUMP:
            data = self._host_dump_capture(system_id)
        else:  # CaptureMethod.KDUMP
            data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete core appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, "vmcore", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id, "vmcore-redacted", self._extract_redacted(data), Sensitivity.REDACTED
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)
```
Add the import `from kdive.domain.capture import CaptureMethod`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_retrieve.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the tool boundary**

`tests/mcp/test_vmcore_tools.py` already exists — follow its idiom (`_pool(migrated_url)`,
`_ctx()`, `seed_crashed_system`, `asyncio.run`, `dict_row`; all already imported there). Append:
```python
def test_fetch_rejects_unsupported_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await vmcore_tools.fetch_vmcore(pool, _ctx(), system_id=sys_id, method="kdump")
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_fetch_records_method_in_dedup_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await vmcore_tools.fetch_vmcore(pool, _ctx(), system_id=sys_id, method="host_dump")
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{sys_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
            assert row is not None and row["n"] == 1

    asyncio.run(_run())
```
Add `from kdive.domain.errors import ErrorCategory` to the test file if not already imported.

**Regression:** the existing `test_fetch_vmcore_crashed_enqueues_job` asserts the dedup key
`f"{sys_id}:capture_vmcore"` (≈ line 123); the new method-suffixed key breaks it — update that
assertion to `f"{sys_id}:capture_vmcore:host_dump"` in the same commit.

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_vmcore_tools.py -q`
Expected: FAIL — `fetch_vmcore` has no `method` kwarg.

- [ ] **Step 7: Implement the tool-boundary change**

In `mcp/tools/vmcore.py`:
- Add `from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod`.
- Change `fetch_vmcore` to accept `method: str = "host_dump"`; parse it to `CaptureMethod` (a bad value → `_config_error`); reject `method not in LOCAL_LIBVIRT_SUPPORTED` or a non-core method (`method not in {HOST_DUMP, KDUMP}`) with `_config_error`.
- Thread `method.value` into the job payload (`{"system_id": system_id, "method": method.value}`) and the dedup key (`f"{system_id}:capture_vmcore:{method.value}"`).
- In `capture_handler`, read `method = CaptureMethod(job.payload["method"])` and call `retriever.capture(system_id, method)`.
- In `register`, add the `method` arg to the `vmcore_fetch` tool wrapper (`Literal["host_dump", "kdump"]`, default `"host_dump"`).

- [ ] **Step 8: Run all touched tests**

Run: `uv run pytest tests/mcp/test_vmcore_tools.py tests/providers/local_libvirt/test_retrieve.py -q`
Expected: PASS.

- [ ] **Step 9: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/mcp/tools/vmcore.py src/kdive/providers/local_libvirt/retrieve.py \
        tests/mcp/test_vmcore_tools.py tests/providers/local_libvirt/test_retrieve.py
git commit -m "feat(vmcore): add capture method arg + supported-set validation"
```

> **Note:** The *provisioned-for* check (`host_dump` requires `preserve_on_crash`) needs the boundary to resolve the System's profile; it lands with Tier 1 (which introduces `preserve_on_crash`'s effect). Here the supported-set + core-method validation is sufficient and tested.

---

## Phase 2 — Tier 0: console capture

### Task 2.1: `render_domain_xml` adds the always-on console + log

**Files:**
- Modify: `src/kdive/providers/local_libvirt/provisioning.py:200-225` (`render_domain_xml`)
- Test: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/local_libvirt/test_provisioning.py`:
```python
from uuid import UUID

# Parse with defusedxml (XXE-safe), matching install.py's _safe_fromstring; stdlib ET
# parsing is vulnerable to XXE/billion-laughs even on self-rendered strings in tests.
from defusedxml.ElementTree import fromstring as safe_fromstring

from kdive.providers.local_libvirt.lifecycle.provisioning import console_log_path, render_domain_xml


def test_domain_xml_has_serial_console_with_log() -> None:
    sid = UUID("00000000-0000-0000-0000-0000000000aa")
    root = safe_fromstring(render_domain_xml(sid, _profile()))  # _profile(): a parsed valid profile
    serial = root.find("./devices/serial[@type='pty']")
    assert serial is not None
    log = serial.find("log")
    assert log is not None
    assert log.get("file") == str(console_log_path(sid))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_provisioning.py::test_domain_xml_has_serial_console_with_log -q`
Expected: FAIL — no `serial` element / `console_log_path` undefined.

- [ ] **Step 3: Implement the console device**

In `provisioning.py`, add a module-level helper and a serial device in `render_domain_xml` (after the `disk`, before `metadata`). Use the `pty`+`<log>` idiom — libvirt tees the pty chardev to the log file; a `type="file"` serial with a separate `<log>` at the same path is redundant and rejected by some libvirt versions:
```python
_CONSOLE_DIR = "/var/lib/kdive/console"


def console_log_path(system_id: UUID) -> Path:
    """The deterministic host path libvirt tees the System's serial console to."""
    return Path(_CONSOLE_DIR) / f"{system_id}.log"
```
```python
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)))
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
```
Add `from pathlib import Path` if not present. (The `_CONSOLE_DIR` must exist and be writable by the libvirt qemu user; creating it is a deploy step, noted in Task 2.4.)

- [ ] **Step 4: Run to verify it passes (and no XML regression)**

Run: `uv run pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/provisioning.py \
        tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(provisioning): render an always-on serial console with a log tee"
```

### Task 2.2: Register the console as a `redacted` artifact on boot-window close

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py` — add the `read_console_log` seam
- Modify: `src/kdive/mcp/tools/runs.py:790-826` (`boot_handler`) — register the artifact in a `finally`
- Test: `tests/providers/local_libvirt/test_install.py`, `tests/mcp/test_runs_tools.py`

- [ ] **Step 1: Write the failing test for the console-read seam**

Append to `tests/providers/local_libvirt/test_install.py`:
```python
from pathlib import Path

from kdive.providers.local_libvirt.lifecycle.install import read_console_log


def test_read_console_log_returns_bytes(tmp_path: Path) -> None:
    log = tmp_path / "sys.log"
    log.write_bytes(b"[ 0.0] Kernel panic - __d_lookup\n")
    assert b"__d_lookup" in read_console_log(log)


def test_read_console_log_missing_is_empty(tmp_path: Path) -> None:
    assert read_console_log(tmp_path / "absent.log") == b""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k read_console_log`
Expected: FAIL — `read_console_log` undefined.

- [ ] **Step 3: Implement the console-read seam**

In `install.py`:
```python
def read_console_log(path: Path) -> bytes:
    """Read the System's console log; absent → empty (boot may not have written).

    A `PermissionError` (the worker cannot read qemu's `0600` log — see Task 2.4's group
    setup) is treated as empty but **logged**, so a permission fault is never a silent
    empty console.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError:
        _log.warning("console log %s not readable by the worker; registering empty", path)
        return b""
```
(`_log` is the module logger already defined in `install.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k read_console_log`
Expected: PASS.

- [ ] **Step 5: Write the failing boot-handler test (registration fires even when boot fails)**

Append to `tests/mcp/test_runs_tools.py`, reusing its real helpers (`_seed_succeeded_run`, `_record_install_step`, `_enqueue_job`, `_FakeBooter(error=…)`, `_count`) exactly as `test_boot_handler_records_step_run_stays_succeeded:1231` does. `ARTIFACTS` has no list method — assert the row with `_count` + raw SQL (the test DB is isolated per `migrated_url`):
```python
def test_boot_handler_registers_console_even_on_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _record_install_step(pool, run_id)
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.BOOT_TIMEOUT)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await runs_tools.boot_handler(conn, job, booter)
            n = await _count(
                pool, "SELECT count(*) AS n FROM artifacts WHERE object_key LIKE %s", ("%/console",)
            )
        assert n == 1

    asyncio.run(_run())
```
(In the unit test `read_console_log` finds no console file → empty bytes, so an empty `redacted` console artifact row is still registered, which is what we assert. `object_store_from_env()` is the same store the build-handler test path already uses, so it is configured in the test env.)

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_runs_tools.py -q -k registers_console`
Expected: FAIL — no console artifact row.

- [ ] **Step 7: Implement registration in `boot_handler`**

In `runs.py`, add imports (`read_console_log`, `console_log_path`, `object_store_from_env`, `Sensitivity`, `Redactor`) and wrap the step call (replacing the bare `await _run_step_locked(conn, run_id, "boot", _do)` at `runs.py:825`):
```python
    try:
        await _run_step_locked(conn, run_id, "boot", _do)
    finally:
        raw = await asyncio.to_thread(read_console_log, console_log_path(run.system_id))
        redacted = Redactor().redact_text(raw.decode("utf-8", "replace")).encode("utf-8")
        stored = await asyncio.to_thread(
            lambda: object_store_from_env().put_artifact(
                "local", "systems", str(run.system_id), "console",
                data=redacted, sensitivity=Sensitivity.REDACTED, retention_class="console",
            )
        )
        async with conn.transaction():  # own transaction: independent of the (possibly failed) step
            if await _existing_console_key(conn, run.system_id) is None:  # idempotent on replay
                await ARTIFACTS.insert(
                    conn,
                    register_artifact_row(stored, owner_kind="systems", owner_id=run.system_id),
                )
    return str(run_id)
```
Add `_existing_console_key` next to it (a `…/console` LIKE query, mirroring `_existing_raw_key` in `vmcore.py:155`). The `finally` re-raises the boot error after registering, so the worker still dead-letters a real `boot_timeout` while the console is captured.

- [ ] **Step 8: Run the handler tests**

Run: `uv run pytest tests/mcp/test_runs_tools.py -q -k boot_handler`
Expected: PASS — registration fires on both the success and the raised paths.

- [ ] **Step 9: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/install.py src/kdive/mcp/tools/runs.py \
        tests/providers/local_libvirt/test_install.py tests/mcp/test_runs_tools.py
git commit -m "feat(boot): register the console log as a redacted artifact on window close"
```

### Task 2.3: `install` — method-conditional `_kdump_check` + optional initrd

The embedded-initramfs kernel (Task 0.3) is a single self-contained artifact with no external
initrd, but `install` currently always fetches an initrd (from `kernel_ref`) and renders
`<initrd>` (`install.py:153-154,221`). Make the initrd optional so the embedded-kernel boot
gets no spurious `<initrd>`, and gate the kdump preflight on the method.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py:141-223` (`install`, `_render_direct_kernel_xml`)
- Test: `tests/providers/local_libvirt/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from uuid import uuid4

from kdive.domain.capture import CaptureMethod
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall, ReadinessResult


class _FakeDomain:
    def XMLDesc(self, _flags):  # noqa: N802, ANN001, ANN202
        return "<domain type='kvm'><name>kdive-x</name><devices/></domain>"


class _FakeConn:
    def __init__(self) -> None:
        self.defined_xml: str | None = None

    def lookupByName(self, _name):  # noqa: N802, ANN001, ANN202
        return _FakeDomain()

    def defineXML(self, xml):  # noqa: N802, ANN001, ANN202
        self.defined_xml = xml
        return _FakeDomain()

    def close(self):  # noqa: ANN202
        return 0


def _kdump_must_not_run(_sid):  # noqa: ANN001, ANN202
    raise AssertionError("kdump_check called for a non-kdump method")


def test_install_skips_kdump_check_and_omits_initrd(tmp_path: Path) -> None:
    conn = _FakeConn()

    def _initrd_must_not_run(_ref, _dest):  # noqa: ANN001, ANN202
        raise AssertionError("initrd fetched when no initrd_ref given")

    installer = LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=lambda _ref, _dest: None,
        fetch_initrd=_initrd_must_not_run,
        kdump_check=_kdump_must_not_run,
        readiness=lambda _sid: ReadinessResult(answered=True, ok=True),
        staging_root=tmp_path,
    )
    # CONSOLE + no initrd_ref: kdump_check skipped, no initrd fetched, no <initrd> rendered.
    installer.install(uuid4(), uuid4(), "ref", cmdline="console=ttyS0", method=CaptureMethod.CONSOLE)
    assert conn.defined_xml is not None
    assert "<initrd>" not in conn.defined_xml
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k kdump_check_and_omits_initrd`
Expected: FAIL — `install()` has no `method`/`initrd_ref` params; it always fetches an initrd and renders `<initrd>`.

- [ ] **Step 3: Implement the conditionals**

Add `method: CaptureMethod` and `initrd_ref: str | None = None` params to `install`. Fetch the
initrd only when given; gate the kdump preflight on the method; pass `initrd_path` (or `None`)
into the renderer:
```python
    def install(
        self, system_id: UUID, run_id: UUID, kernel_ref: str, *,
        cmdline: str, method: CaptureMethod = CaptureMethod.HOST_DUMP, initrd_ref: str | None = None,
    ) -> None:
        staging_dir = self._staging_root / str(system_id) / str(run_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        kernel_path = staging_dir / "kernel"
        self._fetch_kernel(kernel_ref, kernel_path)
        initrd_path: Path | None = None
        if initrd_ref is not None:
            initrd_path = staging_dir / "initrd"
            self._fetch_initrd(initrd_ref, initrd_path)
        if method is CaptureMethod.KDUMP and not self._kdump_check(system_id):
            raise CategorizedError(
                "kdump capture service/initramfs not present on the staged System",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id)},
            )
        # … open conn; pass initrd_path (may be None) to _render_direct_kernel_xml …
```
In `_render_direct_kernel_xml`, render `<initrd>` only when `initrd_path is not None`. The
defaults keep the existing `install_handler` call site (`runs.py`) compiling unchanged — it
need not pass `method` until the provisioned-for check lands (Tier 1), because the kdump
preflight only triggers for `method=kdump`. **Regression check:** any existing
`test_install.py` / `_render_direct_kernel_xml` test that asserts an `<initrd>` element must be
updated to pass an `initrd_ref` (the new no-initrd default omits it); run the full
`tests/providers/local_libvirt/test_install.py` in Step 4 and fix such assertions.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/install.py tests/providers/local_libvirt/test_install.py
git commit -m "feat(install): gate the kdump preflight on method=kdump"
```

### Task 2.4: Live end-to-end (gated)

**Files:**
- Test: `tests/integration/live_stack/test_console_capture.py` (marked for `just test-live`)

**Prerequisites (host, one-time):** Phase 0 complete (self-contained kernel with the embedded initramfs at `~/src/linux/arch/x86_64/boot/bzImage`, placeholder rootfs in the pool); `/var/lib/kdive/console` exists and **the kdive worker can read the serial `<log>` files libvirt's qemu process writes**. qemu writes them `0600 qemu:qemu`, so group membership alone is insufficient (mode `0600` denies group read). Pick one at deploy: run the worker as the `qemu` user; or set the worker's libvirt to write logs world/group-readable; or (cleanest, and what a remote-libvirt provider will need anyway) read the console via the libvirt API instead of the filesystem. If the worker cannot read the file, `read_console_log` logs a warning and registers an empty console — so a misconfiguration is observable, not silent. Phase 0 validates the capture mechanism independently (it writes to world-readable `/tmp`).

- [ ] **Step 1: Write the gated test**

A `@pytest.mark.live_vm` test (match the marker used in `tests/integration/live_stack/`) that drives the real tool sequence twice and contrasts the console artifact:

1. `systems.define` with `rootfs: {kind: "path", path: "/var/lib/libvirt/images/kdive-tier0-rootfs.qcow2"}` and `debug: {}` (Tier 0 needs no debug flags); provision → ready.
2. Create a Run; ingest the build artifact via the external-build upload path (#113): upload the **single self-contained `bzImage`** (initramfs embedded, Task 0.3) as the kernel — **no separate initrd** — with `cmdline` =
   - **vulnerable debug args:** `dhash_entries=1 panic_on_oops=1`
   - **clean (baseline):** `console=ttyS0`
3. `runs.install` (with `method=console`, `initrd_ref=None`) + `runs.boot`. The boot handler registers the console artifact on window close. (Validate the rendered domain XML once with `virt-xml-validate` during host setup.)
4. Read the System's `console` artifact via `artifacts.*`.

Assert: the **vulnerable** run's console contains `__d_lookup`; the **clean** run's console contains `KDIVE-BUSYBOX-READY` and not `__d_lookup`. That contrast is the A/B signal the scoring layer (next session) consumes.

- [ ] **Step 2: Run it on the host**

Run: `just test-live -k console_capture`
Expected: PASS — vulnerable boot's console names `__d_lookup`; clean boot's does not.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/live_stack/test_console_capture.py
git commit -m "test(live): console capture A/B on dhash_entries=1"
```

---

## Self-Review

- **Spec coverage (this plan's scope):** §2 method vocabulary → Task 1.1; §5 profile `debug`/optional `crashkernel` → Task 1.2; §4 `vmcore.fetch` method + supported-set → Task 1.3; §6.1 always-on console XML → Task 2.1; §4/§6.1/§11 console registration in `finally` → Task 2.2; §8 method-conditional `_kdump_check` → Task 2.3; §12.1/§12.4 console A/B → Task 2.4. §13.1–4 → Phase 0. **Deferred (own plans):** §6.2/§7/§8 panic-escalation + host_dump capture (Tier 1); §6.3/§9 gdbstub port allocation + resolver (Tier 2); §5 provisioned-for check (lands with Tier 1).
- **Placeholder scan / real-helper check:** Phase 0 steps are exact commands; the probe XML is inlined. All target test files were verified to exist (`test_vmcore_tools.py`, `test_retrieve.py`, `test_install.py`, `test_runs_tools.py`) except `tests/domain/test_capture.py` (Created in 1.1). Tests reuse the modules' real helpers — `_pool`/`_ctx`/`seed_crashed_system`/`dict_row` (vmcore), `_FakeStore`/`_SYS` (retrieve), `_seed_succeeded_run`/`_record_install_step`/`_enqueue_job`/`_FakeBooter(error=…)`/`_count` (runs) — not invented fixtures. `ARTIFACTS` exposes only `insert`, so row assertions use raw SQL via `_count`.
- **Regression flags (existing tests the changes break, fixed in the same commit):** the dedup-key suffix breaks `test_fetch_vmcore_crashed_enqueues_job`; the required `capture(method)` breaks three `test_capture_*` tests; the optional-initrd default may break any `_render_direct_kernel_xml` test asserting `<initrd>`. Each is called out in its task.
- **Type consistency:** `CaptureMethod` (Task 1.1) is the type used in `capture()` (1.3), `install()` (2.3), and the handlers; `console_log_path()` (2.1) is consumed by `read_console_log` (2.2). `read_vmcore_build_id`/`extract_redacted` seam names match `retrieve.py`.

---

## Follow-on plans (after Phase 0)

- **Tier 1 — host_dump:** panic-escalation cmdline (§8), `_host_dump_capture` seam (§7, branch on §13.4), `_real_read_vmcore_build_id`/`_real_extract_redacted` (branch on §13.3), `_await_ready` crashed-success outcome + `READY→CRASHED` handler transition (§8), provisioned-for check (§5).
- **Tier 2 — gdbstub:** atomic port allocation + persistence on the System (needs a migration) + release on teardown (§6.3), QEMU `-gdb` passthrough in `render_domain_xml`, `_real_resolve_endpoint` reading the persisted port (§9).
