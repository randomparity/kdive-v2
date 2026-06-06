# Install readiness + kdump-check seam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `install.py`'s two `MISSING_DEPENDENCY` stubs with a real console-classifier readiness probe (`live_vm` tail wrapper over a pure host-free core) and a host-observable initrd-presence kdump gate, so the live demo (#123) can verify a vulnerable boot fails and a fixed boot is ready.

**Architecture:** A pure `classify_console(bytes) -> "ready"|"crashed"|"pending"` is the unit-testable core (mirrors the G2 fetch seam's `_stage_object`); `_real_readiness` is the thin `live_vm` wrapper that tails the truncated console log, polls `virsh domstate`, and maps the verdict to `ReadinessResult`. The injected `kdump_check` seam is removed and replaced by an inline `_kdump_capture_present(initrd_path)` host check in `install()`.

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`. Commands via the `justfile` (`just lint`, `just type`, `just test`). Spec: `docs/superpowers/specs/2026-06-06-install-readiness-kdump-seam-design.md`; ADR-0055.

---

## File structure

- `src/kdive/providers/local_libvirt/install.py` — add the classifier + constants + types; add `_real_readiness`/`_domain_exited` (`live_vm`); replace the `kdump_check` seam with `_kdump_capture_present`; remove `KdumpCheck`/`_real_kdump_check`; update `__init__`/`from_env`/`install()`.
- `tests/providers/local_libvirt/test_install.py` — classifier unit tests; fixture-classification test; rewrite the two kdump tests + the `_install`/`_Readiness` helpers to the new contract; fill the `live_vm` acceptance.
- `tests/providers/local_libvirt/fixtures/console_crash_dhash.log` (new) — representative crash console.
- `tests/providers/local_libvirt/fixtures/console_clean_ready.log` (new) — representative clean console.
- `tests/adversarial/test_provider_xml.py` — drop `kdump_check=` from the `_installer` helper; add the truncate-default `<log>` XML guard.

---

## Task 1: Console classifier (pure core) + constants

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py`
- Test: `tests/providers/local_libvirt/test_install.py`

- [ ] **Step 1: Write the failing tests**

First add `classify_console` to the **existing** install import block in
`tests/providers/local_libvirt/test_install.py` (lines 17-21) — it already imports
`LocalLibvirtInstall, ReadinessResult, _stage_object`, and the file already has `import pytest`
(line 12) and `import os` (line 5), so do **not** add either again:

```python
from kdive.providers.local_libvirt.install import (
    LocalLibvirtInstall,
    ReadinessResult,
    _stage_object,
    classify_console,
)
```

Then add these classifier tests (top-level; `_MARKER` is a new module constant):

```python
_MARKER = "kdive-ready"


@pytest.mark.parametrize(
    "signature_line",
    [
        "[   22.10] Kernel panic - not syncing: Attempted to kill init!",
        "[   22.10] watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [udevd:142]",
        "[   22.10] Oops: 0000 [#1] PREEMPT SMP",
        "[   22.10] general protection fault: 0000 [#1] SMP",
        "[   22.10] Unable to handle kernel paging request at virtual address 0",
        "[   22.10] BUG: KASAN: slab-out-of-bounds in __d_lookup+0x1a/0x2b",
        "[   22.10] BUG: KFENCE: use-after-free read in d_lookup",
        "[   22.10] rcu: INFO: rcu_sched self-detected stall on CPU",
    ],
)
def test_classify_crash_signatures_resolve_crashed(signature_line: str) -> None:
    data = f"[    0.00] booting\n{signature_line}\n  __d_lookup+0x1a\n".encode()
    assert classify_console(data, marker=_MARKER) == "crashed"


def test_classify_marker_line_alone_is_ready() -> None:
    data = b"[    0.00] booting\n[    3.40] systemd: reached target\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_empty_is_pending() -> None:
    assert classify_console(b"", marker=_MARKER) == "pending"


def test_classify_no_marker_no_crash_is_pending() -> None:
    data = b"[    0.00] Linux version 7.0.0\n[    1.10] still booting\n"
    assert classify_console(data, marker=_MARKER) == "pending"


def test_classify_debug_substring_is_not_a_crash() -> None:
    # `(?<![A-Za-z])BUG:` must not match `DEBUG:` (no false crash on a benign line).
    data = b"[    1.0] app DEBUG: BUG: this is a benign log token\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_crash_before_marker_wins() -> None:
    data = b"[    1.0] Kernel panic - not syncing\n[    2.0] late\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "crashed"


def test_classify_signature_after_marker_stays_ready() -> None:
    # Pre-marker scoping: a signature *after* the marker line does not flip a healthy boot.
    data = b"kdive-ready\n[    4.0] some-daemon: BUG: benign post-marker chatter\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_systemd_unit_line_is_not_the_marker() -> None:
    # Whole-line match: `Starting kdive-ready.service` contains the substring but is not the signal.
    data = b"[    3.2] systemd[1]: Starting kdive-ready.service - KDIVE marker...\n"
    assert classify_console(data, marker=_MARKER) == "pending"


def test_classify_malformed_utf8_does_not_raise() -> None:
    data = b"\xff\xfe partial \x80 bytes, still booting\n"
    assert classify_console(data, marker=_MARKER) == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k classify -q`
Expected: FAIL — `ImportError: cannot import name 'classify_console'`.

- [ ] **Step 3: Implement the classifier + constants**

In `src/kdive/providers/local_libvirt/install.py`, add `import re` to the stdlib import block and `Literal` to the `typing` import (`from typing import Literal, NamedTuple, Protocol`). After the existing module constants (`_DEFAULT_BOOT_WINDOW_POLLS = 30`) add:

```python
_READINESS_MARKER = "kdive-ready"
# Fatal/stall-grade kernel crash signatures (ADR-0055 §4). Fail-closed and additive.
# The lookbehinds keep `BUG:`/`Oops:` from matching benign substrings (e.g. `DEBUG:`).
_CRASH_SIGNATURE = re.compile(
    r"Kernel panic"
    r"|(?<![A-Za-z])BUG:"
    r"|(?<![A-Za-z])Oops:"
    r"|general protection fault"
    r"|[Uu]nable to handle kernel"
    r"|KASAN:"
    r"|KFENCE:"
    r"|detected stall"
)

ConsoleVerdict = Literal["ready", "crashed", "pending"]
```

Then add the pure classifier near `read_console_log` (above `_ObjectReader`):

```python
def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
    """Classify a console capture: did the System reach the marker, crash, or neither?

    The marker is matched as a whole line — the readiness unit echoes the bare line
    ``kdive-ready`` to the console, while systemd's ``Starting kdive-ready.service`` line
    (same substring) is not the signal (ADR-0055 §3). A crash signature (§4) in the
    pre-marker region wins (crash-wins, fail-closed). Bytes are decoded utf-8 with
    ``errors="replace"`` so a partial multibyte tail or non-UTF-8 console never raises.

    Returns:
        ``"crashed"`` if a crash signature precedes the marker (or the marker is absent),
        ``"ready"`` if a bare marker line is present with no crash before it, else
        ``"pending"``.
    """
    text = data.decode("utf-8", errors="replace")
    marker_re = re.compile(rf"^[^\S\n]*{re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    marker_match = marker_re.search(text)
    region = text if marker_match is None else text[: marker_match.start()]
    if _CRASH_SIGNATURE.search(region):
        return "crashed"
    return "ready" if marker_match is not None else "pending"
```

Add `"classify_console"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k classify -q`
Expected: PASS (all `classify_*` cases).

- [ ] **Step 5: Run guardrails**

Run: `just lint && just type`
Expected: clean (zero warnings).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt/install.py tests/providers/local_libvirt/test_install.py
git commit -m "feat(install): add the pure console readiness classifier (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Committed console fixtures + fixture-classification test

**Files:**
- Create: `tests/providers/local_libvirt/fixtures/console_crash_dhash.log`
- Create: `tests/providers/local_libvirt/fixtures/console_clean_ready.log`
- Test: `tests/providers/local_libvirt/test_install.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/providers/local_libvirt/test_install.py`:

```python
_FIXTURES = Path(__file__).parent / "fixtures"


def test_crash_fixture_classifies_crashed() -> None:
    data = (_FIXTURES / "console_crash_dhash.log").read_bytes()
    assert classify_console(data) == "crashed"


def test_clean_fixture_classifies_ready() -> None:
    data = (_FIXTURES / "console_clean_ready.log").read_bytes()
    assert classify_console(data) == "ready"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k fixture -q`
Expected: FAIL — `FileNotFoundError` for the fixtures.

- [ ] **Step 3: Create the crash fixture**

Create `tests/providers/local_libvirt/fixtures/console_crash_dhash.log` (a representative `dhash_entries=1` soft-lockup with `__d_lookup` in the backtrace; no `kdive-ready` line — the kernel hangs before userspace). Replace with a real captured log when the `live_vm` acceptance runs.

```
[    0.000000] Linux version 7.0.0 (kdive@demo) #1 SMP PREEMPT_DYNAMIC
[    0.000000] Command line: console=ttyS0 dhash_entries=1
[    0.512345] Dentry cache hash table entries: 1 (order: 0, 8 bytes, linear)
[    1.004221] Run /sbin/init as init process
[   22.103442] watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [systemd-udevd:142]
[   22.103442] CPU: 0 PID: 142 Comm: systemd-udevd Not tainted 7.0.0 #1
[   22.103442] Call Trace:
[   22.103442]  __d_lookup+0x4a/0x120
[   22.103442]  d_lookup+0x35/0x70
[   22.103442]  lookup_fast+0x6c/0x150
[   22.103442]  walk_component+0x2b/0x180
```

- [ ] **Step 4: Create the clean fixture**

Create `tests/providers/local_libvirt/fixtures/console_clean_ready.log` (boots to the marker; includes a `DEBUG:` token pre-marker, the systemd `kdive-ready.service` status line pre-marker, the bare echo, and a benign `BUG:` substring post-marker — locking in the lookbehind, whole-line match, and pre-marker scoping all at once).

```
[    0.000000] Linux version 7.0.0 (kdive@demo) #1 SMP PREEMPT_DYNAMIC
[    0.000000] Command line: console=ttyS0
[    0.512345] Dentry cache hash table entries: 262144 (order: 9, 2097152 bytes, linear)
[    1.004221] Run /sbin/init as init process
[    2.220110] systemd-udevd DEBUG: BUG: benign pre-marker log token
[    3.200400] systemd[1]: Starting kdive-ready.service - KDIVE readiness marker...
kdive-ready
[    3.401200] systemd[1]: Started kdive-ready.service.
[    3.510000] some-daemon[321]: BUG: benign post-marker chatter, must not flip the verdict
[    3.600000] systemd[1]: Startup finished.
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k fixture -q`
Expected: PASS (crash → `crashed`, clean → `ready`).

- [ ] **Step 6: Run guardrails**

Run: `just lint && just type && just test -k "classify or fixture"`
Expected: clean; tests pass. (Confirm no `check for added large files` hook trips — the fixtures are a few hundred bytes.)

- [ ] **Step 7: Commit**

```bash
git add tests/providers/local_libvirt/fixtures/ tests/providers/local_libvirt/test_install.py
git commit -m "test(install): committed crash+clean console fixtures classified in CI (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Replace the kdump_check seam with a host initrd-presence gate

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py` (type alias line ~76, `__init__` ~117-134, `from_env` ~149-156, `install()` gate ~195, `_real_kdump_check` ~375-380)
- Modify: `tests/providers/local_libvirt/test_install.py` (`_Readiness` ~48-60, `_install` ~73-90, the two kdump tests ~144-167, `test_install_skips_kdump_check_and_omits_initrd` ~289-315)
- Modify: `tests/adversarial/test_provider_xml.py` (`_installer` ~102-109)

- [ ] **Step 1: Rewrite the kdump unit tests to the initrd-presence contract**

In `tests/providers/local_libvirt/test_install.py`:

(a) In the `_Readiness` dataclass remove the `kdump_present` field and the `kdump_check` method (leave only `answered`/`ok` and the `readiness` method):

```python
@dataclass
class _Readiness:
    """Canned readiness seam. answered=False → never-answered; ok=False → answered-fail."""

    answered: bool = True
    ok: bool = True

    def readiness(self, system_id: UUID) -> ReadinessResult:
        return ReadinessResult(answered=self.answered, ok=self.ok)
```

(b) In `_install` drop the `kdump_check=seam.kdump_check,` line from the `LocalLibvirtInstall(...)` call.

(c) Replace `test_install_kdump_absent_is_config_error_before_redefine` and `test_install_kdump_present_proceeds` with the initrd-presence contract:

```python
def test_install_kdump_without_initrd_is_config_error_before_redefine(tmp_path: Path) -> None:
    # method=KDUMP with no initrd_ref: the capture initramfs is absent → CONFIGURATION_ERROR,
    # nothing redefined (the crashkernel reservation is inert without a capture initrd).
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, method=CaptureMethod.KDUMP)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []


def test_install_kdump_with_initrd_proceeds(tmp_path: Path) -> None:
    # method=KDUMP with a staged initrd present: install proceeds and redefines once.
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(
        _SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, method=CaptureMethod.KDUMP, initrd_ref=_INITRD_REF
    )
    assert len(conn.defined_xml) == 1
```

(d) Replace `test_install_skips_kdump_check_and_omits_initrd` (it asserted the kdump_check seam was not called — the seam no longer exists). Keep its initrd-omission coverage:

```python
def test_install_console_method_omits_initrd(tmp_path: Path) -> None:
    """CONSOLE method, no initrd_ref: no initrd fetched, no <initrd> element rendered."""

    def _initrd_must_not_run(_ref: str, _dest: Path) -> None:
        raise AssertionError("initrd fetched when no initrd_ref given")

    conn = _conn_with_existing()
    installer = LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=lambda _ref, _dest: None,
        fetch_initrd=_initrd_must_not_run,
        readiness=lambda _sid: ReadinessResult(answered=True, ok=True),
        staging_root=tmp_path,
    )
    installer.install(
        _SYS, _RUN, _KERNEL_REF, cmdline="console=ttyS0", method=CaptureMethod.CONSOLE
    )
    assert len(conn.defined_xml) == 1
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    assert os_el.find("initrd") is None
```

- [ ] **Step 2: Update the adversarial XML test's installer helper**

In `tests/adversarial/test_provider_xml.py`, drop the `kdump_check=lambda system_id: True,` line from the `_installer` `LocalLibvirtInstall(...)` call (lines ~103-109).

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py tests/adversarial/test_provider_xml.py -q`

Expected: FAIL. **This is a coupled refactor, not a clean feature red** — Steps 1-2 dropped the
`kdump_check=` argument from the `_install`/`_installer` call sites, but the constructor still
*requires* the `kdump_check` parameter (it is not removed until Step 4). So **every** test built
through `_install(...)` / `_installer(...)` errors at construction with
`TypeError: LocalLibvirtInstall.__init__() missing 1 required keyword-only argument: 'kdump_check'`.
That broad TypeError *is* the expected red here — it confirms the call sites no longer pass the
seam. The clean category check (`CONFIGURATION_ERROR` for kdump-without-initrd) only becomes
observable after Step 4 removes the parameter and adds the gate; do not expect a
`MISSING_DEPENDENCY`-vs-`CONFIGURATION_ERROR` signal at this step (construction fails first).

- [ ] **Step 4: Implement the gate in install.py**

In `src/kdive/providers/local_libvirt/install.py`:

(a) Remove the `KdumpCheck` type alias line: `type KdumpCheck = Callable[[UUID], bool]`.

(b) In `__init__`, remove the `kdump_check: KdumpCheck,` parameter and the `self._kdump_check = kdump_check` assignment.

(c) In `from_env`, remove the `kdump_check=_real_kdump_check,` argument.

(d) Add the pure helper near `_real_fetch` (replacing the `_real_kdump_check` stub):

```python
def _kdump_capture_present(initrd_path: Path | None) -> bool:
    """Host-observable kdump prerequisite: a separate capture initramfs was staged.

    A ``crashkernel=`` reservation is inert without a capture initramfs (ADR-0030 §4).
    This is necessary, not sufficient — it does not prove the initrd is kdump-capable;
    the in-guest verification lands with #115 (ADR-0055 §5). An embedded-initramfs kernel
    (``initrd_ref=None`` → ``initrd_path is None``) is rejected for kdump (the M0 boundary).
    """
    return initrd_path is not None and initrd_path.exists()
```

Delete the `_real_kdump_check` function entirely.

(e) In `install()`, change the gate:

```python
        if method is CaptureMethod.KDUMP and not _kdump_capture_present(initrd_path):
            raise CategorizedError(
                "kdump capture initramfs not staged (a separate initrd is required for kdump)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id)},
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py tests/adversarial/test_provider_xml.py -q`
Expected: PASS. (`CaptureMethod` import in test_install stays; `_Readiness` no longer needs `kdump_check`.)

- [ ] **Step 6: Run guardrails**

Run: `just lint && just type`
Expected: clean — confirm no `Callable` import is now unused in `install.py` (it is still used by `Fetch`/`Connect`/`Readiness` types, so it stays). `ty` whole-tree must be green.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/local_libvirt/install.py tests/providers/local_libvirt/test_install.py tests/adversarial/test_provider_xml.py
git commit -m "feat(install): gate kdump on a staged capture initrd, drop the kdump_check seam (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire `_real_readiness` + `_domain_exited` (live_vm)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py` (imports; constants; replace the `_real_readiness` stub ~383-388)

This task replaces the `_real_readiness` `MISSING_DEPENDENCY` stub with the real `live_vm` tail wrapper. The wrapper is `# pragma: no cover - live_vm` (no host in CI), so there is no host-free unit test for it; its logic is covered by the classifier tests (Task 1), the fixtures (Task 2), and the `live_vm` acceptance (Task 6). Do **not** add a non-`live_vm` test that spawns `virsh` or sleeps.

- [ ] **Step 1: Add the constants and imports**

In `src/kdive/providers/local_libvirt/install.py` add to the stdlib import block:

```python
import subprocess  # noqa: S404 - virsh domstate is invoked with a fixed argv, no shell
import time
```

Add `console_log_path` to the provisioning import:

```python
from kdive.providers.local_libvirt.provisioning import console_log_path, domain_name_for
```

Add next to `_DEFAULT_BOOT_WINDOW_POLLS` (with a comment that their product is the window, ADR-0055 §7):

```python
# The boot window is _DEFAULT_BOOT_WINDOW_POLLS × _POLL_INTERVAL_SECONDS = 150s (ADR-0055 §7):
# boot()._await_ready loops the poll count; _real_readiness owns the per-poll cadence below.
_POLL_INTERVAL_SECONDS = 5.0
_DOMSTATE_PROBE_TIMEOUT = 10
_TERMINAL_DOMSTATES = frozenset({"shut off", "crashed"})
```

- [ ] **Step 2: Replace the `_real_readiness` stub and add `_domain_exited`**

Replace the existing `_real_readiness` stub function with:

```python
def _domain_exited(domain_name: str) -> bool:  # pragma: no cover - live_vm
    """True only if ``virsh domstate`` reports a terminal state (shut off / crashed).

    A probe error/timeout or a transient non-running state (``paused``, ``in shutdown``)
    is not proof of exit (v1: a flaky/slow probe keeps waiting), so it returns ``False``
    and the caller keeps polling (ADR-0055 §7).
    """
    uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["virsh", "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.stdout.strip().lower() in _TERMINAL_DOMSTATES


def _real_readiness(system_id: UUID) -> ReadinessResult:  # pragma: no cover - live_vm
    """One run-readiness probe of the System's truncated console (ADR-0055 §6/§7).

    A single per-poll probe — ``boot()._await_ready`` drives the repetition. Classify the
    console: a marker line → ``answered, ok``; a pre-marker crash signature → ``answered,
    not ok`` (resolved early, not waited out). On ``pending``, a *terminally exited* guest
    (after a final re-read that still finds neither) is ``answered, not ok`` (v1's
    ``exited``); a still-running guest sleeps one poll interval and stays unanswered, so the
    boot window (poll count × interval) elapses as ``boot_timeout`` if it never comes up.
    """
    log_path = console_log_path(system_id)
    verdict = classify_console(read_console_log(log_path))
    if verdict == "ready":
        return ReadinessResult(answered=True, ok=True)
    if verdict == "crashed":
        return ReadinessResult(answered=True, ok=False)
    if _domain_exited(domain_name_for(system_id)):
        if classify_console(read_console_log(log_path)) == "ready":
            return ReadinessResult(answered=True, ok=True)
        return ReadinessResult(answered=True, ok=False)
    time.sleep(_POLL_INTERVAL_SECONDS)
    return ReadinessResult(answered=False, ok=False)
```

- [ ] **Step 3: Run the full unit suite to verify no regression**

Run: `just test`
Expected: PASS. (`_real_readiness`/`_domain_exited` are `live_vm`-only and not exercised here; the injected fakes still drive `boot()`.)

- [ ] **Step 4: Run guardrails**

Run: `just lint && just type`
Expected: clean. Confirm `subprocess`/`time`/`console_log_path` are all used (no unused-import F401).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/install.py
git commit -m "feat(install): implement the live_vm console-tail readiness probe (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Truncate-default `<log>` XML regression guard

**Files:**
- Test: `tests/adversarial/test_provider_xml.py`

This pins the §3/§8 precondition: pre-marker scoping is sound only because the console `<log>` is truncated per `create()`. A future change adding `append='on'` must fail this guard.

- [ ] **Step 1: Write the failing test**

Add to `tests/adversarial/test_provider_xml.py`. The file already imports `render_domain_xml`,
`ET`, `_SYS`, and has a working `_profile(...)` helper (lines 50-70) that builds a valid
`ProvisioningProfile` via `ProvisioningProfile.parse({...})` — reuse it exactly as the existing
`test_render_domain_xml_…` does (line 89). Do **not** invent a `_MINIMAL_PROFILE` dict or call
`model_validate` (neither exists; the model is built with `.parse`):

```python
def test_console_log_element_does_not_enable_append() -> None:
    # Readiness pre-marker scoping (ADR-0055 §3/§8) relies on the console log being
    # truncated per create(). QEMU/libvirt default logappend=off; the rendered <log> must
    # not set append='on', or a stale prior-boot marker could survive into the next boot.
    domain = ET.fromstring(render_domain_xml(_SYS, _profile()))  # noqa: S314 - self-rendered
    logs = domain.findall("./devices/serial/log")
    assert logs, "the always-on serial console <log> tee must be present (ADR-0049 §4)"
    for log in logs:
        assert log.get("append") != "on"
```

- [ ] **Step 2: Run the test to verify it passes (guard is already satisfied)**

Run: `uv run python -m pytest tests/adversarial/test_provider_xml.py -k append -q`
Expected: PASS — `provisioning.py:232` emits `<log file=...>` with no `append`, so the guard holds. To prove the guard *bites*, temporarily add `append="on"` to that `ET.SubElement(serial, "log", ...)` call, re-run, confirm FAIL, then revert.

- [ ] **Step 3: Run guardrails**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/adversarial/test_provider_xml.py
git commit -m "test(install): guard the console-log truncate default for readiness scoping (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Fill the `live_vm` readiness acceptance

**Files:**
- Modify: `tests/providers/local_libvirt/test_install.py` (`test_live_vm_real_install_boot` ~404-413)

The full build→install→boot A/B (vulnerable vs fixed) is the integration epic's harness (#123); #127 owns the readiness *mechanism*. Make the gated test exercise the real `_real_readiness`/`_domain_exited`/console path against an operator-provided, already-installed System, and skip cleanly when the host/System is absent — never error or un-gate.

- [ ] **Step 1: Replace the stub body**

Replace `test_live_vm_real_install_boot` with:

```python
@pytest.mark.live_vm
def test_live_vm_real_install_boot() -> None:  # pragma: no cover - live_vm
    import shutil

    uri = os.environ.get("KDIVE_LIBVIRT_URI")
    system_id = os.environ.get("KDIVE_LIVE_VM_SYSTEM_ID")
    if not uri or not shutil.which("virsh") or not system_id:
        pytest.skip("KDIVE_LIBVIRT_URI, virsh, or KDIVE_LIVE_VM_SYSTEM_ID unavailable")
    # The operator points KDIVE_LIVE_VM_SYSTEM_ID at a System already provisioned + installed
    # with a kdive-ready rootfs (epic #123 build/install harness). boot() power-cycles it and
    # drives the real _real_readiness console probe; a clean kdive-ready boot resolves without
    # raising. The vulnerable-vs-fixed A/B is exercised host-free by the committed crash/clean
    # fixtures (test_*_fixture_classifies_*) and end-to-end by the #123 integration harness.
    booter = LocalLibvirtInstall.from_env()
    booter.boot(UUID(system_id))  # no raise == readiness resolved ok at the marker
```

- [ ] **Step 2: Verify it skips cleanly without a host**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_install.py -k live_vm -q`
Expected: SKIPPED (the `live_vm` marker is deselected by `just test`; running it directly skips on the missing env). It must never ERROR.

- [ ] **Step 3: Run the full gate**

Run: `just ci`
Expected: green — lint, type, lint-shell, lint-workflows, check-mermaid, test all pass; `live_vm` deselected.

- [ ] **Step 4: Commit**

```bash
git add tests/providers/local_libvirt/test_install.py
git commit -m "test(install): exercise the real readiness probe under the live_vm gate (#127)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes (spec coverage)

- Classifier verdict taxonomy + pre-marker scoping + whole-line marker + crash signatures + decode safety → Task 1. ✓ (spec §3, §4, §4.1)
- Committed crash/clean fixtures as the CI falsifiability guard → Task 2. ✓ (spec §7)
- Host initrd-presence kdump gate, seam removal, embedded-initramfs boundary → Task 3. ✓ (spec §5)
- `_real_readiness` per-poll probe, pinned window constants, `domstate` exit guards, final re-read → Task 4. ✓ (spec §6)
- Truncate-default precondition guard → Task 5. ✓ (spec §3/§8)
- `live_vm` acceptance (real probe, skips cleanly) → Task 6. ✓ (spec §7)
- Redaction: none needed — the seam returns booleans only; the console artifact is redacted by `runs.boot` (unchanged). ✓ (spec §3)
- No committed golden/schema is invalidated (no model/OpenAPI/insta change). ✓
