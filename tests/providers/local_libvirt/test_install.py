"""LocalLibvirtInstall provider tests — injected fakes, no live host (ADR-0030)."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET  # noqa: S405 - parses only self-rendered, trusted test XML
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import FetchedArtifact
from kdive.providers.local_libvirt import install
from kdive.providers.local_libvirt.install import (
    LocalLibvirtInstall,
    ReadinessResult,
    _stage_object,
    _verdict_to_result,
    classify_console,
)
from kdive.providers.ports import InstallRequest
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn

_SYS = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_KERNEL_REF = "local/runs/22222222-2222-2222-2222-222222222222/kernel"
_INITRD_REF = "local/runs/22222222-2222-2222-2222-222222222222/initrd"
_CMDLINE = "console=ttyS0 crashkernel=256M"


@dataclass
class _Fetch:
    """Records (kernel_ref/marker, dest) and writes canned bytes via temp-then-rename."""

    calls: list[tuple[str, Path]] = field(default_factory=list)
    fail: bool = False

    def __call__(self, ref: str, dest: Path) -> None:
        self.calls.append((ref, dest))
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(b"canned")
        if self.fail:
            raise CategorizedError("synthetic fetch failure", category=ErrorCategory.STALE_HANDLE)
        tmp.rename(dest)


@dataclass
class _Readiness:
    """Canned readiness seam. answered=False → never-answered; ok=False → answered-fail."""

    answered: bool = True
    ok: bool = True
    probe_error: str | None = None

    def readiness(self, system_id: UUID) -> ReadinessResult:
        return ReadinessResult(answered=self.answered, ok=self.ok, probe_error=self.probe_error)


def _existing_domain() -> FakeDomain:
    """The domain provisioning already defined (no <os> direct-kernel section yet)."""
    return FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS))


def _conn_with_existing(*, define_error: int | None = None) -> FakeLibvirtConn:
    domain = _existing_domain()
    return FakeLibvirtConn(lookup={domain.domain_name: domain}, define_error=define_error)


def _install(
    *,
    conn: FakeLibvirtConn,
    fetch: _Fetch | None = None,
    seam: _Readiness | None = None,
    staging_root: Path,
) -> LocalLibvirtInstall:
    fetch = fetch or _Fetch()
    seam = seam or _Readiness()
    return LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=fetch,
        fetch_initrd=fetch,
        readiness=seam.readiness,
        staging_root=staging_root,
        boot_window_polls=3,
    )


def _request(
    *,
    cmdline: str = _CMDLINE,
    method: CaptureMethod = CaptureMethod.HOST_DUMP,
    initrd_ref: str | None = None,
) -> InstallRequest:
    return InstallRequest(
        system_id=_SYS,
        run_id=_RUN,
        kernel_ref=_KERNEL_REF,
        cmdline=cmdline,
        method=method,
        initrd_ref=initrd_ref,
    )


# --- install: render + staging -------------------------------------------------------


def test_install_redefines_direct_kernel_os(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    assert len(conn.defined_xml) == 1
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    kernel = os_el.find("kernel")
    initrd = os_el.find("initrd")
    cmdline = os_el.find("cmdline")
    assert kernel is not None and initrd is not None and cmdline is not None
    assert cmdline.text == _CMDLINE
    # The kernel/initrd point at the per-Run staging path …/{system_id}/{run_id}/….
    assert kernel.text is not None and f"{_SYS}/{_RUN}" in kernel.text
    assert initrd.text is not None and f"{_SYS}/{_RUN}" in initrd.text


def test_install_stages_kernel_and_initrd_to_per_run_path(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    fetch = _Fetch()
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert (staged_dir / "kernel").exists()
    assert (staged_dir / "initrd").exists()
    # No leftover temp file from the temp-then-rename.
    assert list(staged_dir.glob("*.part")) == []


def test_install_does_not_inject_xml_from_cmdline(tmp_path: Path) -> None:
    # A hostile cmdline value must be carried as text, not parsed as markup.
    hostile = "crashkernel=256M </cmdline><evil/>"
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(cmdline=hostile))
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None and os_el.find("evil") is None  # not injected
    cmdline = os_el.find("cmdline")
    assert cmdline is not None and cmdline.text == hostile  # carried verbatim


# --- install: kdump prerequisite -----------------------------------------------------


def test_install_kdump_without_initrd_is_config_error_before_redefine(tmp_path: Path) -> None:
    # method=KDUMP with no initrd_ref: the capture initramfs is absent → CONFIGURATION_ERROR,
    # nothing redefined (the crashkernel reservation is inert without a capture initrd).
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.KDUMP))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []  # nothing redefined on a missing capture path


def test_install_kdump_with_initrd_proceeds(tmp_path: Path) -> None:
    # method=KDUMP with a staged initrd present: install proceeds and redefines once.
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(method=CaptureMethod.KDUMP, initrd_ref=_INITRD_REF))
    assert len(conn.defined_xml) == 1  # redefined once, no CONFIGURATION_ERROR raised


# --- install: failures ---------------------------------------------------------------


def test_install_definexml_error_is_install_failure(tmp_path: Path) -> None:
    conn = _conn_with_existing(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request())
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


def test_install_fetch_failure_leaves_no_final_file(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    fetch = _Fetch(fail=True)
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path)
    with pytest.raises(CategorizedError):
        inst.install(_request())
    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert not (staged_dir / "kernel").exists()  # rename never happened


# --- boot: power-cycle + readiness ---------------------------------------------------


def _domain(*, active: bool = False) -> FakeDomain:
    return FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS), active=active)


def test_boot_powercycles_running_domain_then_readiness(tmp_path: Path) -> None:
    domain = _domain(active=True)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.boot(_SYS)  # no raise
    assert domain.calls == ["destroy", "create"]  # running → destroy then create


def test_boot_starts_stopped_domain(tmp_path: Path) -> None:
    domain = _domain(active=False)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.boot(_SYS)
    assert domain.calls == ["create"]  # not running → just create


def test_boot_never_answered_is_boot_timeout(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT


def test_boot_timeout_includes_first_readiness_probe_error(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False, probe_error="virsh domstate timed out after 2s")
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)

    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)

    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT
    assert caught.value.details["probe_error"] == "virsh domstate timed out after 2s"


def test_boot_answered_but_failed_is_readiness_failure(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=True, ok=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.READINESS_FAILURE


def test_boot_create_error_is_install_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name=f"kdive-{_SYS}",
        system_id=str(_SYS),
        raise_on={"create": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


def test_boot_absent_domain_is_install_failure(tmp_path: Path) -> None:
    conn = FakeLibvirtConn(lookup={})
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


# --- from_env does not connect/spawn -------------------------------------------------


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")

    def _no_open(*_: object, **__: object) -> object:
        raise AssertionError("from_env must not open a libvirt connection")

    monkeypatch.setattr(libvirt, "open", _no_open)
    inst = LocalLibvirtInstall.from_env()  # building must not connect
    assert isinstance(inst, LocalLibvirtInstall)


# --- read_console_log ----------------------------------------------------------------


def test_read_console_log_returns_bytes(tmp_path: Path) -> None:
    from kdive.providers.local_libvirt.install import read_console_log

    log = tmp_path / "sys.log"
    log.write_bytes(b"[ 0.0] Kernel panic - __d_lookup\n")
    assert b"__d_lookup" in read_console_log(log)


def test_read_console_log_missing_is_empty(tmp_path: Path) -> None:
    from kdive.providers.local_libvirt.install import read_console_log

    assert read_console_log(tmp_path / "absent.log") == b""


# --- method-conditional kdump + optional initrd --------------------------------------


def test_install_console_method_omits_initrd(tmp_path: Path) -> None:
    """CONSOLE method, no initrd_ref: no initrd fetched; no <initrd> in XML."""

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
    # CONSOLE + no initrd_ref: no initrd fetched, no <initrd> rendered.
    installer.install(_request(cmdline="console=ttyS0", method=CaptureMethod.CONSOLE))
    assert len(conn.defined_xml) == 1
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    assert os_el.find("initrd") is None


# --- _stage_object: object-store read → temp-then-rename ------------------------------


@dataclass
class _FakeStore:
    """Records the (ref, etag) of each get_artifact and returns canned bytes or raises."""

    data: bytes = b"bzimage-bytes"
    error: CategorizedError | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.calls.append((key, etag))
        if self.error is not None:
            raise self.error
        return FetchedArtifact(self.data, Sensitivity.SENSITIVE, "build")


def test_stage_object_writes_bytes_via_temp_then_rename(tmp_path: Path) -> None:
    store = _FakeStore(data=b"real-kernel")
    dest = tmp_path / "kernel"

    _stage_object(store, _KERNEL_REF, dest)

    assert dest.read_bytes() == b"real-kernel"
    # The temp file is renamed into place, never left behind.
    assert list(tmp_path.iterdir()) == [dest]


def test_stage_object_reads_unconditionally_with_none_etag(tmp_path: Path) -> None:
    store = _FakeStore()

    _stage_object(store, _KERNEL_REF, tmp_path / "kernel")

    # ADR-0054 regression guard: the seam must read with etag=None (an empty/non-None etag
    # would 412 on a real store). This is the only place the etag argument is chosen.
    assert store.calls == [(_KERNEL_REF, None)]


def test_stage_object_propagates_store_error_and_leaves_dest_intact(tmp_path: Path) -> None:
    dest = tmp_path / "kernel"
    dest.write_bytes(b"previously-staged")
    store = _FakeStore(
        error=CategorizedError("gone", category=ErrorCategory.STALE_HANDLE),
    )

    with pytest.raises(CategorizedError) as excinfo:
        _stage_object(store, _KERNEL_REF, dest)

    assert excinfo.value.category is ErrorCategory.STALE_HANDLE
    # A failed fetch leaves the prior file untouched and no partial temp behind.
    assert dest.read_bytes() == b"previously-staged"
    assert list(tmp_path.iterdir()) == [dest]


def test_stage_object_categorizes_local_write_failure(tmp_path: Path) -> None:
    dest = tmp_path / "kernel"
    # A directory at the .part path makes write_bytes raise IsADirectoryError (an OSError),
    # standing in for a disk-full/permission staging-write fault.
    (tmp_path / "kernel.part").mkdir()
    store = _FakeStore(data=b"kernel-bytes")

    with pytest.raises(CategorizedError) as excinfo:
        _stage_object(store, _KERNEL_REF, dest)

    # The local write fault is a categorized infrastructure failure, not a raw OSError.
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["dest"] == str(dest)
    assert not dest.exists()


def test_install_categorizes_staging_mkdir_failure(tmp_path: Path) -> None:
    # A regular file where the per-System staging dir must be makes mkdir(parents=True) fail.
    (tmp_path / str(_SYS)).write_bytes(b"not-a-dir")
    inst = _install(conn=_conn_with_existing(), staging_root=tmp_path)

    with pytest.raises(CategorizedError) as excinfo:
        inst.install(_request(initrd_ref=_INITRD_REF))

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["op"] == "mkdir"


# --- live_vm real redefine + boot ----------------------------------------------------


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


# --- classify_console: the pure readiness verdict core (ADR-0055) --------------------

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
    data = b"[    1.0] app DEBUG: initializing the readiness subsystem\nkdive-ready\n"
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


_FIXTURES = Path(__file__).parent / "fixtures"


def test_verdict_to_result_crashed_is_answered_failure() -> None:
    # The demo's load-bearing signal: a crashed verdict must resolve to readiness failure.
    assert _verdict_to_result("crashed", exited=False) == ReadinessResult(answered=True, ok=False)


def test_verdict_to_result_ready_is_answered_ok() -> None:
    assert _verdict_to_result("ready", exited=False) == ReadinessResult(answered=True, ok=True)


def test_verdict_to_result_pending_running_keeps_polling() -> None:
    # A still-booting guest is not yet answered → None tells the probe to keep polling.
    assert _verdict_to_result("pending", exited=False) is None


def test_verdict_to_result_pending_exited_is_answered_failure() -> None:
    # A guest that exited without reaching the marker is answered-but-failed (v1's `exited`).
    assert _verdict_to_result("pending", exited=True) == ReadinessResult(answered=True, ok=False)


def test_real_readiness_treats_missing_domain_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "read_console_log", lambda path: b"")
    monkeypatch.setattr(install, "_domain_exit_probe", lambda name: install._DomainExitProbe(True))

    result = install._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is True
    assert result.ok is False


def test_domain_exited_treats_missing_kdive_domain_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def domstate_missing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["virsh"],
            returncode=1,
            stdout="",
            stderr="error: failed to get domain 'kdive-22222222-2222-2222-2222-222222222222'",
        )

    monkeypatch.setattr(install.subprocess, "run", domstate_missing)

    assert install._domain_exited("kdive-22222222-2222-2222-2222-222222222222") is True


def test_real_readiness_reports_domstate_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def domstate_timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["virsh"], timeout=2)

    monkeypatch.setattr(install, "read_console_log", lambda path: b"")
    monkeypatch.setattr(install.time, "sleep", lambda _: None)
    monkeypatch.setattr(install.subprocess, "run", domstate_timeout)

    result = install._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is False
    assert result.probe_error == "virsh domstate timed out after 2s"


def test_crash_fixture_classifies_crashed() -> None:
    data = (_FIXTURES / "console_crash_dhash.log").read_bytes()
    assert classify_console(data) == "crashed"


def test_clean_fixture_classifies_ready() -> None:
    data = (_FIXTURES / "console_clean_ready.log").read_bytes()
    assert classify_console(data) == "ready"
