"""LocalLibvirtInstall provider tests — injected fakes, no live host (ADR-0030)."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET  # noqa: S405 - parses only self-rendered, trusted test XML
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.providers.local_libvirt.install import (
    LocalLibvirtInstall,
    ReadinessResult,
    _stage_object,
)
from kdive.store.objectstore import FetchedArtifact
from tests.providers.local_libvirt.conftest import FakeDomain, FakeLibvirtConn

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
    """Canned readiness/kdump seam. answered=False → never-answered; ok=False → answered-fail."""

    answered: bool = True
    ok: bool = True
    kdump_present: bool = True

    def kdump_check(self, system_id: UUID) -> bool:
        return self.kdump_present

    def readiness(self, system_id: UUID) -> ReadinessResult:
        return ReadinessResult(answered=self.answered, ok=self.ok)


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
        kdump_check=seam.kdump_check,
        readiness=seam.readiness,
        staging_root=staging_root,
        boot_window_polls=3,
    )


# --- install: render + staging -------------------------------------------------------


def test_install_redefines_direct_kernel_os(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, initrd_ref=_INITRD_REF)

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
    inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, initrd_ref=_INITRD_REF)

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
    inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=hostile)
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None and os_el.find("evil") is None  # not injected
    cmdline = os_el.find("cmdline")
    assert cmdline is not None and cmdline.text == hostile  # carried verbatim


# --- install: kdump prerequisite -----------------------------------------------------


def test_install_kdump_absent_is_config_error_before_redefine(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    seam = _Readiness(kdump_present=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, method=CaptureMethod.KDUMP)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []  # nothing redefined on a missing capture path


def test_install_kdump_present_proceeds(tmp_path: Path) -> None:
    # method=KDUMP with the capture path present: install proceeds and redefines the domain.
    conn = _conn_with_existing()
    seam = _Readiness(kdump_present=True)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    inst.install(
        _SYS,
        _RUN,
        _KERNEL_REF,
        cmdline=_CMDLINE,
        method=CaptureMethod.KDUMP,
        initrd_ref=_INITRD_REF,
    )
    assert len(conn.defined_xml) == 1  # redefined once, no CONFIGURATION_ERROR raised


# --- install: failures ---------------------------------------------------------------


def test_install_definexml_error_is_install_failure(tmp_path: Path) -> None:
    conn = _conn_with_existing(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


def test_install_fetch_failure_leaves_no_final_file(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    fetch = _Fetch(fail=True)
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path)
    with pytest.raises(CategorizedError):
        inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE)
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


def test_install_skips_kdump_check_and_omits_initrd(tmp_path: Path) -> None:
    """CONSOLE method: kdump_check never called; no initrd fetched; no <initrd> in XML."""

    def _kdump_must_not_run(_sid: UUID) -> bool:
        raise AssertionError("kdump_check called for a non-kdump method")

    def _initrd_must_not_run(_ref: str, _dest: Path) -> None:
        raise AssertionError("initrd fetched when no initrd_ref given")

    conn = _conn_with_existing()
    installer = LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=lambda _ref, _dest: None,
        fetch_initrd=_initrd_must_not_run,
        kdump_check=_kdump_must_not_run,
        readiness=lambda _sid: ReadinessResult(answered=True, ok=True),
        staging_root=tmp_path,
    )
    # CONSOLE + no initrd_ref: kdump_check skipped, no initrd fetched, no <initrd> rendered.
    installer.install(
        _SYS, _RUN, _KERNEL_REF, cmdline="console=ttyS0", method=CaptureMethod.CONSOLE
    )
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
        inst.install(_SYS, _RUN, _KERNEL_REF, cmdline=_CMDLINE, initrd_ref=_INITRD_REF)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["op"] == "mkdir"


# --- live_vm real redefine + boot ----------------------------------------------------


@pytest.mark.live_vm
def test_live_vm_real_install_boot() -> None:  # pragma: no cover - live_vm
    import shutil

    uri = os.environ.get("KDIVE_LIBVIRT_URI")
    if not uri or not shutil.which("virsh"):
        pytest.skip("KDIVE_LIBVIRT_URI or virsh unavailable")
    # The real redefine + power-cycle + readiness preflight runs against the operator-provided
    # libvirt host; wired by the live_vm runner as part of the #19 gated suite.
    raise NotImplementedError("live_vm real install/boot harness wired by the live_vm runner")
