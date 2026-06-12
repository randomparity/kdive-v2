"""Tests for the local-libvirt Provisioning plane (ADR-0025)."""

from __future__ import annotations

import copy
import importlib
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import libvirt
import pytest
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers import libvirt_xml as libvirt_xml_contract
from kdive.providers.libvirt_xml import KDIVE_METADATA_NS, parse_metadata_system_id
from kdive.providers.local_libvirt.lifecycle import provisioning as provisioning_module
from kdive.providers.local_libvirt.lifecycle import storage as storage_module
from kdive.providers.local_libvirt.lifecycle.provisioning import (
    LocalLibvirtProvisioning,
    ProvisioningFiles,
    console_log_path,
    domain_name_for,
    overlay_path,
    render_domain_xml,
)
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from tests.providers.local_libvirt.fakes import libvirt_error

_SYS = UUID("11111111-1111-1111-1111-111111111111")
_DISK = "/var/lib/kdive/rootfs/fedora-40.qcow2"

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
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _profile(**overrides: Any) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID)
    data["provider"]["local-libvirt"].update(overrides)
    return ProvisioningProfile.parse(data)


def _render(
    system_id: UUID = _SYS,
    profile: ProvisioningProfile | None = None,
    *,
    disk_path: str = _DISK,
) -> str:
    return render_domain_xml(system_id, profile or _profile(), disk_path=disk_path)


def test_domain_name_is_kdive_prefixed() -> None:
    assert domain_name_for(_SYS) == "kdive-11111111-1111-1111-1111-111111111111"


def test_import_does_not_register_elementtree_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(libvirt_xml_contract, "_kdive_namespace_registered", False)

    def fake_register_namespace(prefix: str, uri: str) -> None:
        calls.append((prefix, uri))

    monkeypatch.setattr(ET, "register_namespace", fake_register_namespace)
    reloaded = importlib.reload(provisioning_module)

    assert calls == []

    reloaded.render_domain_xml(_SYS, _profile(), disk_path=_DISK)
    reloaded.render_domain_xml(_SYS, _profile(), disk_path=_DISK)

    assert calls == [("kdive", KDIVE_METADATA_NS)]


def test_render_carries_name_memory_vcpu_machine_and_rootfs() -> None:
    root = _safe_fromstring(_render())
    assert root.findtext("name") == "kdive-11111111-1111-1111-1111-111111111111"
    assert root.findtext("memory") == "4096"
    assert root.findtext("vcpu") == "4"
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "x86_64"
    assert os_type.get("machine") == "pc-q35-9.0"
    source = root.find("devices/disk/source")
    assert source is not None
    assert source.get("file") == "/var/lib/kdive/rootfs/fedora-40.qcow2"


def test_render_declares_qcow2_disk_driver() -> None:
    # The rootfs images are qcow2; a driver-less disk makes libvirt default to raw, so the guest
    # reads the qcow2 header instead of the ext4 filesystem and panics unable to mount root.
    root = _safe_fromstring(_render())
    driver = root.find("devices/disk/driver")
    assert driver is not None
    assert driver.get("name") == "qemu"
    assert driver.get("type") == "qcow2"


def test_render_emits_deterministic_uuid_for_idempotent_redefine() -> None:
    # defineXML redefines an existing domain only when the XML carries its uuid; a deterministic
    # uuid = system_id lets a provision retry redefine the running domain in place instead of
    # failing with "domain already exists with uuid ..." on the name collision.
    root = _safe_fromstring(_render())
    assert root.findtext("uuid") == str(_SYS)


def test_required_cmdline_root_matches_the_rendered_disk_target() -> None:
    # ADR-0061: the platform-injected root= must name the device provisioning attaches. These are
    # set independently in two modules; this guards them moving together.
    from kdive.domain.capture import CaptureMethod
    from kdive.services.runs.steps import system_required_cmdline

    target = _safe_fromstring(_render()).find("devices/disk/target")
    assert target is not None
    assert f"root=/dev/{target.get('dev')}" in system_required_cmdline(CaptureMethod.CONSOLE)


def test_render_uses_disk_path_override_when_given() -> None:
    # provision() attaches a per-System overlay, not the shared base, by passing disk_path.
    root = _safe_fromstring(_render(disk_path="/var/lib/kdive/rootfs/ov.qcow2"))
    source = root.find("devices/disk/source")
    assert source is not None and source.get("file") == "/var/lib/kdive/rootfs/ov.qcow2"


def test_render_has_no_kernel_or_cmdline() -> None:
    # The kdump crashkernel reservation is the install/boot plane's job (#17), not provision's.
    root = _safe_fromstring(_render())
    assert root.find("os/kernel") is None
    assert root.find("os/cmdline") is None


def test_render_metadata_tag_round_trips_through_discovery() -> None:
    root = _safe_fromstring(_render())
    tag = root.find(f"metadata/{{{KDIVE_METADATA_NS}}}system")
    assert tag is not None
    assert parse_metadata_system_id(ET.tostring(tag, encoding="unicode")) == str(_SYS)


def test_render_defaults_machine_when_absent() -> None:
    root = _safe_fromstring(_render(profile=_profile(domain_xml_params={})))
    os_type = root.find("os/type")
    assert os_type is not None and os_type.get("machine") == "q35"


def test_validate_profile_rejects_unknown_domain_xml_param() -> None:
    with pytest.raises(CategorizedError) as caught:
        LocalLibvirtProfilePolicy().validate_profile(
            _profile(domain_xml_params={"machine": "q35", "bogus": "x"})
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_render_rejects_unknown_domain_xml_param() -> None:
    # render re-checks at the worker boundary (a hand-built jsonb that bypassed the tool).
    with pytest.raises(CategorizedError):
        _render(profile=_profile(domain_xml_params={"nope": "x"}))


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
    closed: int = 0
    recorded_xml: list[str] = field(default_factory=list)  # each defineXML payload, in order

    def defineXML(self, xml: str) -> _ProvDomain:
        if self.define_error is not None:
            raise libvirt_error(self.define_error)
        self.recorded_xml.append(xml)
        name = _safe_fromstring(xml).findtext("name")
        assert name is not None
        return self.defined.setdefault(name, _ProvDomain(name))

    def lookupByName(self, name: str) -> _ProvDomain:
        if self.lookup_error is not None:
            raise libvirt_error(self.lookup_error)
        if name not in self.defined:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.defined[name]

    def close(self) -> int:
        self.closed += 1
        return 0


def _prov(
    conn: _ProvConn,
    *,
    make_overlay: Callable[[str, str], None] = lambda _base, _overlay: None,
    remove_overlay: Callable[[str], None] = lambda _overlay: None,
    overlay_exists: Callable[[str], bool] = lambda _overlay: False,
) -> LocalLibvirtProvisioning:
    # The overlay seams default to no-ops so the libvirt-only tests never spawn qemu-img; the
    # console-log seam is also a no-op so they never depend on host /var/lib/kdive permissions.
    # The default "overlay absent" makes provision create one, matching a fresh provision.
    return LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=make_overlay,
            remove_overlay=remove_overlay,
            overlay_exists=overlay_exists,
            prepare_console_log=lambda _path: None,
        ),
        materialize_rootfs=lambda rootfs, _system_id: (
            rootfs.path if rootfs.kind == "local" else "/var/lib/kdive/rootfs/upload.qcow2"
        ),
    )


def test_prov_helper_does_not_prepare_host_console_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_mkdir(self: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise AssertionError("unit helper must not touch host console log paths")

    monkeypatch.setattr(provisioning_module.Path, "mkdir", fail_mkdir)

    _prov(_ProvConn()).provision(_SYS, _profile())


def test_provision_defines_and_starts_returns_name() -> None:
    conn = _ProvConn()
    name = _prov(conn).provision(_SYS, _profile())
    assert name == "kdive-11111111-1111-1111-1111-111111111111"
    assert conn.defined[name].created is True
    assert conn.closed == 1  # the connection is closed after use (no leak)


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


def test_provision_real_create_failure_undefines_domain() -> None:
    # A real start failure (not "already running") must undefine the domain `defineXML` just
    # registered, so provision is transactional — no defined-but-unstarted domain is leaked.
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    conn = _ProvConn(defined={name: dom})
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert dom.undefined is True  # the defined-but-unstarted domain was cleaned up
    assert conn.closed == 1


def test_provision_already_running_domain_does_not_undefine() -> None:
    # "already running" (OPERATION_INVALID) is the desired post-state — the live domain must
    # NOT be undefined.
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).provision(_SYS, _profile())
    assert dom.undefined is False  # kept the running domain


def test_provision_already_running_domain_is_idempotent() -> None:
    # A retry after a partial provision: defineXML redefines, create() reports "already
    # running" (OPERATION_INVALID) — the desired post-state, not a failure.
    name = domain_name_for(_SYS)
    conn = _ProvConn(
        defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)}
    )
    assert _prov(conn).provision(_SYS, _profile()) == name  # no raise
    assert conn.closed == 1


def test_teardown_destroys_and_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.destroyed is True and dom.undefined is True
    assert conn.closed == 1  # the connection is closed after use (no leak)


def test_provision_creates_overlay_over_base_and_attaches_it() -> None:
    # The disk attached to the domain is a per-System overlay backed by the resolved base, so two
    # Systems never contend for the base's qcow2 write lock and guest state does not bleed.
    made: list[tuple[str, str]] = []
    conn = _ProvConn()
    _prov(conn, make_overlay=lambda base, ov: made.append((base, ov))).provision(_SYS, _profile())
    base, overlay = made[0]
    assert base == "/var/lib/kdive/rootfs/fedora-40.qcow2"  # the _VALID base
    assert overlay == overlay_path(_SYS)
    disk = _safe_fromstring(conn.recorded_xml[0]).find("devices/disk/source")
    assert disk is not None and disk.get("file") == overlay  # the domain boots the overlay


def test_provision_prepares_console_log_before_define() -> None:
    calls: list[tuple[str, str]] = []

    def prepare(path: Path) -> None:
        calls.append(("prepare", path.name))

    class RecordingConn(_ProvConn):
        def defineXML(self, xml: str) -> _ProvDomain:  # noqa: N802 - mirrors libvirt binding
            calls.append(("define", "xml"))
            return super().defineXML(xml)

    conn = RecordingConn()
    LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=lambda _base, _overlay: None,
            overlay_exists=lambda _overlay: False,
            prepare_console_log=prepare,
        ),
        materialize_rootfs=lambda _rootfs, _system_id: "/var/lib/kdive/rootfs/base.qcow2",
    ).provision(_SYS, _profile())

    assert calls == [("prepare", f"{_SYS}.log"), ("define", "xml")]


def test_real_make_overlay_timeout_is_provisioning_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["qemu-img"], timeout=storage_module._QEMU_IMG_TIMEOUT_S)

    monkeypatch.setattr(storage_module.subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert caught.value.details["timeout_s"] == storage_module._QEMU_IMG_TIMEOUT_S


def test_real_make_overlay_missing_qemu_img_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("qemu-img")

    monkeypatch.setattr(storage_module.subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
    }


def test_real_make_overlay_launch_oserror_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fork_failed(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise OSError("fork failed")

    monkeypatch.setattr(storage_module.subprocess, "run", _fork_failed)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
        "error": "OSError",
    }


def test_real_remove_overlay_oserror_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unlink_failed(self: object, *, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise PermissionError("permission denied")

    monkeypatch.setattr(storage_module.Path, "unlink", _unlink_failed)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_remove_overlay("/rootfs/overlay.qcow2")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {
        "op": "remove_overlay",
        "overlay": "overlay.qcow2",
        "error": "PermissionError",
    }


def test_teardown_removes_the_overlay() -> None:
    removed: list[str] = []
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name)})
    _prov(conn, remove_overlay=removed.append).teardown(name)
    assert removed == [overlay_path(_SYS)]


def test_teardown_removes_overlay_even_when_domain_already_gone() -> None:
    # The overlay must be reclaimed regardless of whether the domain still exists.
    removed: list[str] = []
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_NO_DOMAIN)
    _prov(conn, remove_overlay=removed.append).teardown(domain_name_for(_SYS))
    assert removed == [overlay_path(_SYS)]


def test_provision_skips_overlay_create_when_it_already_exists() -> None:
    # Idempotent retry of an already-running System: the overlay QEMU still holds open must NOT
    # be recreated (qemu-img would fail the lock or truncate the live disk). provision skips the
    # create when the overlay is present and still reaches the already-running success post-state.
    def _boom(_base: str, _overlay: str) -> None:
        raise AssertionError("make_overlay must not run when the overlay already exists")

    name = domain_name_for(_SYS)
    conn = _ProvConn(
        defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)}
    )
    prov = _prov(conn, make_overlay=_boom, overlay_exists=lambda _overlay: True)
    assert prov.provision(_SYS, _profile()) == name  # no raise, no overlay recreate


def test_provision_create_failure_removes_the_overlay() -> None:
    # A real start failure must reclaim the overlay it just created, mirroring the domain undefine,
    # so a failed provision leaks neither a defined domain nor an overlay file.
    removed: list[str] = []
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})
    with pytest.raises(CategorizedError):
        _prov(conn, remove_overlay=removed.append).provision(_SYS, _profile())
    assert removed == [overlay_path(_SYS)]


def test_provision_cleanup_failure_preserves_start_failure_category() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})

    def fail_remove(_overlay: str) -> None:
        raise CategorizedError(
            "synthetic overlay cleanup failure",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )

    with pytest.raises(CategorizedError) as caught:
        _prov(conn, remove_overlay=fail_remove).provision(_SYS, _profile())

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_console_log_failure_removes_the_overlay() -> None:
    removed: list[str] = []
    conn = _ProvConn()

    def fail_prepare(_path: Path) -> None:
        raise CategorizedError(
            "synthetic console log failure",
            category=ErrorCategory.PROVISIONING_FAILURE,
        )

    with pytest.raises(CategorizedError) as caught:
        LocalLibvirtProvisioning(
            connect=lambda: conn,
            files=ProvisioningFiles(
                make_overlay=lambda _base, _overlay: None,
                remove_overlay=removed.append,
                overlay_exists=lambda _overlay: False,
                prepare_console_log=fail_prepare,
            ),
            materialize_rootfs=lambda _rootfs, _system_id: "/var/lib/kdive/rootfs/base.qcow2",
        ).provision(_SYS, _profile())

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert removed == [overlay_path(_SYS)]
    assert conn.recorded_xml == []


def test_provision_failure_keeps_preexisting_overlay() -> None:
    # A retry can fail after finding an existing overlay. That overlay may belong to a live or
    # recoverable previous attempt, so this call must not remove a file it did not create.
    removed: list[str] = []
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError):
        _prov(
            conn,
            remove_overlay=removed.append,
            overlay_exists=lambda _overlay: True,
        ).provision(_SYS, _profile())
    assert removed == []


def test_provision_failure_still_closes_connection() -> None:
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError):
        _prov(conn).provision(_SYS, _profile())
    assert conn.closed == 1  # closed even on a libvirt failure


def test_teardown_absent_domain_closes_connection() -> None:
    conn = _ProvConn()
    _prov(conn).teardown(domain_name_for(_SYS))  # NO_DOMAIN -> early return
    assert conn.closed == 1  # the finally still closes


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


def test_reprovision_tears_down_then_redefines_same_name() -> None:
    # Reprovision-in-place (ADR-0038): destroy+undefine the current domain, then define+start
    # the new profile under the SAME deterministic domain name (same system_id).
    name = domain_name_for(_SYS)
    old = _ProvDomain(name)
    conn = _ProvConn(defined={name: old})
    result = _prov(conn).reprovision(_SYS, _profile())
    assert result == name  # same domain name (same system_id)
    assert old.destroyed is True and old.undefined is True  # prior install wiped (destructive)
    assert conn.defined[name].created is True  # the new domain is defined and started


def test_reprovision_on_absent_domain_still_provisions() -> None:
    # A reprovision whose prior domain is already gone (e.g. a retry after a partial wipe)
    # tears down idempotently (NO_DOMAIN swallowed) and provisions the new install.
    conn = _ProvConn()
    name = _prov(conn).reprovision(_SYS, _profile())
    assert conn.defined[name].created is True


def test_reprovision_define_failure_is_provisioning_failure() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name)}, define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).reprovision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    prov = LocalLibvirtProvisioning.from_env()  # building must not open a connection
    assert isinstance(prov, LocalLibvirtProvisioning)


def test_domain_xml_has_serial_console_with_log() -> None:
    # Parse with defusedxml (XXE-safe), matching install.py's _safe_fromstring; stdlib ET
    # parsing is vulnerable to XXE/billion-laughs even on self-rendered strings in tests.
    sid = UUID("00000000-0000-0000-0000-0000000000aa")
    root = _safe_fromstring(_render(system_id=sid))
    serial = root.find("./devices/serial[@type='pty']")
    assert serial is not None
    log = serial.find("log")
    assert log is not None
    assert log.get("file") == str(console_log_path(sid))
    # The paired <console> redirect is what makes the serial device usable.
    console = root.find("./devices/console[@type='pty']")
    assert console is not None
    target = console.find("target")
    assert target is not None
    assert target.get("type") == "serial"
    assert target.get("port") == "0"
