"""Remote-libvirt provisioning over the injected TLS connection (ADR-0080)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import libvirt
import pytest
from defusedxml.ElementTree import fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.provisioning import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    RemoteLibvirtProvision,
    allocate_gdb_port,
    overlay_volume_name,
    recorded_gdb_port,
    render_domain_xml,
    render_volume_xml,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error

SYSTEM_ID = UUID("00000000-0000-0000-0000-00000000beef")


def _remote_profile(**section_overrides: Any) -> ProvisioningProfile:
    section: dict[str, Any] = {
        "base_image_volume": "kdive-base-fedora-42.qcow2",
        "crashkernel": "256M",
        **section_overrides,
    }
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {"remote-libvirt": section},
        }
    )


def test_overlay_volume_name_is_system_scoped() -> None:
    assert overlay_volume_name(SYSTEM_ID) == f"kdive-{SYSTEM_ID}-overlay.qcow2"


def test_render_domain_xml_carries_agent_channel_gdb_and_metadata() -> None:
    xml = render_domain_xml(
        SYSTEM_ID,
        _remote_profile(),
        pool="kdive-pool",
        volume=overlay_volume_name(SYSTEM_ID),
        gdb_addr="10.0.0.5",
        gdb_port=47001,
    )
    root = fromstring(xml)
    assert root.findtext("./name") == f"kdive-{SYSTEM_ID}"
    # Deterministic uuid = the System id: defineXML redefines in place on retry.
    assert root.findtext("./uuid") == str(SYSTEM_ID)
    assert root.findtext("./memory") == "4096"
    assert root.findtext("./vcpu") == "4"
    assert root.find("./os/boot[@dev='hd']") is not None
    channel_target = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    assert channel_target is not None
    assert channel_target.get("type") == "virtio"
    args = [
        arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    assert args == ["-gdb", "tcp:10.0.0.5:47001"]
    assert root.findtext(f"./metadata/{{{KDIVE_METADATA_NS}}}system") == str(SYSTEM_ID)
    disk_source = root.find("./devices/disk/source")
    assert disk_source is not None
    assert disk_source.get("pool") == "kdive-pool"
    assert disk_source.get("volume") == overlay_volume_name(SYSTEM_ID)
    driver = root.find("./devices/disk/driver")
    assert driver is not None
    assert driver.get("type") == "qcow2"
    # No worker-local <log> tee: the path would be on the remote host (ADR-0080).
    assert root.find("./devices/serial/log") is None
    assert root.find("./devices/serial") is not None
    assert root.find("./devices/console") is not None
    # A virtio NIC: the in-guest artifact channel (presigned GET/PUT) needs guest egress.
    nic_source = root.find("./devices/interface[@type='network']/source")
    assert nic_source is not None
    assert nic_source.get("network") == "default"
    nic_model = root.find("./devices/interface/model")
    assert nic_model is not None
    assert nic_model.get("type") == "virtio"
    # i440fx by default: q35 root-port D3cold leaves the virtio disk inaccessible.
    os_type = root.find("./os/type")
    assert os_type is not None
    assert os_type.get("machine") == "pc"


def test_render_domain_xml_uses_configured_machine() -> None:
    xml = render_domain_xml(
        SYSTEM_ID,
        _remote_profile(),
        pool="kdive-pool",
        volume=overlay_volume_name(SYSTEM_ID),
        gdb_addr="10.0.0.5",
        gdb_port=47001,
        machine="q35",
    )
    os_type = fromstring(xml).find("./os/type")
    assert os_type is not None
    assert os_type.get("machine") == "q35"


def test_render_domain_xml_uses_configured_network() -> None:
    xml = render_domain_xml(
        SYSTEM_ID,
        _remote_profile(),
        pool="kdive-pool",
        volume=overlay_volume_name(SYSTEM_ID),
        gdb_addr="10.0.0.5",
        gdb_port=47001,
        network="lab-net",
    )
    root = fromstring(xml)
    nic_source = root.find("./devices/interface[@type='network']/source")
    assert nic_source is not None
    assert nic_source.get("network") == "lab-net"


def test_render_domain_xml_requires_remote_section() -> None:
    local = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://example/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/f.qcow2"}
                }
            },
        }
    )
    with pytest.raises(CategorizedError) as excinfo:
        render_domain_xml(
            SYSTEM_ID, local, pool="p", volume="v", gdb_addr="10.0.0.5", gdb_port=47001
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_recorded_gdb_port_roundtrip() -> None:
    xml = render_domain_xml(
        SYSTEM_ID,
        _remote_profile(),
        pool="p",
        volume="v",
        gdb_addr="10.0.0.5",
        gdb_port=47007,
    )
    assert recorded_gdb_port(xml) == 47007


@pytest.mark.parametrize(
    "xml",
    [
        "<domain><name>x</name></domain>",  # no qemu:commandline
        "not xml at all",
        "<domain xmlns:q='http://libvirt.org/schemas/domain/qemu/1.0'>"
        "<q:commandline><q:arg value='-gdb'/><q:arg value='tcp:1.2.3.4:notaport'/>"
        "</q:commandline></domain>",  # malformed port
        "<domain xmlns:q='http://libvirt.org/schemas/domain/qemu/1.0'>"
        "<q:commandline><q:arg value='-gdb'/></q:commandline></domain>",  # dangling -gdb
    ],
)
def test_recorded_gdb_port_tolerates_absent_or_malformed(xml: str) -> None:
    assert recorded_gdb_port(xml) is None


def test_allocate_lowest_free_port_skips_used() -> None:
    used = {"kdive-a": 47000, "kdive-b": 47002}
    assert allocate_gdb_port(used, own_name="kdive-new", port_min=47000, port_max=47005) == 47001


def test_allocate_reuses_own_recorded_port() -> None:
    used = {"kdive-a": 47000, "kdive-b": 47002}
    assert allocate_gdb_port(used, own_name="kdive-a", port_min=47000, port_max=47005) == 47000


def test_allocate_ignores_own_out_of_range_port() -> None:
    # A recorded port outside the (narrowed) configured range is not reused.
    used = {"kdive-a": 9999}
    assert allocate_gdb_port(used, own_name="kdive-a", port_min=47000, port_max=47005) == 47000


def test_allocate_exhausted_range_raises_provisioning_failure() -> None:
    used = {"kdive-a": 47000, "kdive-b": 47001}
    with pytest.raises(CategorizedError) as excinfo:
        allocate_gdb_port(used, own_name="kdive-new", port_min=47000, port_max=47001)
    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert "47000" in str(excinfo.value.details)


def test_allocate_excludes_tried_ports() -> None:
    # The bounded start-failure advance never re-picks a port it already tried.
    used = {"kdive-a": 47000}
    assert (
        allocate_gdb_port(
            used, own_name="kdive-new", port_min=47000, port_max=47005, exclude={47001}
        )
        == 47002
    )


def test_render_volume_xml_backing_store() -> None:
    xml = render_volume_xml(
        "kdive-X-overlay.qcow2", capacity_bytes=42, backing_path="/pool/base.qcow2"
    )
    root = fromstring(xml)
    assert root.findtext("./name") == "kdive-X-overlay.qcow2"
    assert root.findtext("./capacity") == "42"
    assert root.findtext("./backingStore/path") == "/pool/base.qcow2"
    backing_format = root.find("./backingStore/format")
    assert backing_format is not None
    assert backing_format.get("type") == "qcow2"
    target_format = root.find("./target/format")
    assert target_format is not None
    assert target_format.get("type") == "qcow2"


def test_allocate_does_not_reuse_own_excluded_port() -> None:
    # After a start failure the System's own recorded port is in `exclude` and
    # must not be re-picked by the reuse fast-path.
    used = {"kdive-a": 47000}
    assert (
        allocate_gdb_port(used, own_name="kdive-a", port_min=47000, port_max=47005, exclude={47000})
        == 47001
    )


# --- RemoteLibvirtProvision orchestration over fakes ---------------------------------


class FakeVolume:
    def __init__(
        self, name: str, *, capacity: int = 10 * 2**30, pool: FakePool | None = None
    ) -> None:
        self.volume_name = name
        self._capacity = capacity
        self.pool = pool

    def path(self) -> str:
        return f"/pool/{self.volume_name}"

    def info(self) -> list[int]:
        return [0, self._capacity, 0]

    def delete(self, flags: int = 0) -> int:
        assert self.pool is not None
        self.pool.volumes.pop(self.volume_name, None)
        self.pool.deleted.append(self.volume_name)
        return 0


class FakePool:
    def __init__(self, volumes: dict[str, FakeVolume] | None = None) -> None:
        self.volumes = volumes if volumes is not None else {}
        self.created_xml: list[str] = []
        self.create_error: libvirt.libvirtError | None = None
        self.deleted: list[str] = []
        for volume in self.volumes.values():
            volume.pool = self

    def storageVolLookupByName(self, name: str) -> FakeVolume:  # noqa: N802
        if name in self.volumes:
            return self.volumes[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> FakeVolume:  # noqa: N802
        if self.create_error is not None:
            raise self.create_error
        self.created_xml.append(xml)
        name = fromstring(xml).findtext("./name") or "unnamed"
        volume = FakeVolume(name, pool=self)
        self.volumes[name] = volume
        return volume


class FakeDomain:
    def __init__(self, conn: FakeProvisionConn, xml: str) -> None:
        self.conn = conn
        self.xml = xml
        parsed_name = fromstring(xml).findtext("./name")
        assert parsed_name is not None
        self.domain_name = parsed_name
        self.active = False
        self.agent_states = list(conn.agent_script.get(self.domain_name, []))
        self.active_states = list(conn.active_script.get(self.domain_name, []))
        self.xml_error: libvirt.libvirtError | None = None
        self.destroy_error: libvirt.libvirtError | None = None
        self.destroyed = False
        self.undefined = False

    def name(self) -> str:
        return self.domain_name

    def create(self) -> int:
        result = self.conn.create_results.pop(0) if self.conn.create_results else None
        if result is not None:
            raise result
        self.active = True
        return 0

    def isActive(self) -> int:  # noqa: N802
        if self.active_states:
            self.active = self.active_states.pop(0)
        return 1 if self.active else 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        if self.xml_error is not None:
            raise self.xml_error
        state = (
            self.agent_states.pop(0)
            if self.agent_states
            else ("connected" if self.active else "disconnected")
        )
        root = fromstring(self.xml)
        target = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
        if target is not None:
            target.set("state", state)
            return ET.tostring(root, encoding="unicode")
        return self.xml

    def destroy(self) -> int:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed = True
        self.active = False
        return 0

    def undefine(self) -> int:
        self.conn.domains.pop(self.domain_name, None)
        self.undefined = True
        return 0


class FakeProvisionConn:
    def __init__(self, pools: dict[str, FakePool] | None = None) -> None:
        self.pools = pools if pools is not None else {"default": FakePool()}
        self.domains: dict[str, FakeDomain] = {}
        self.create_results: list[libvirt.libvirtError | None] = []
        self.agent_script: dict[str, list[str]] = {}
        self.active_script: dict[str, list[bool]] = {}
        self.defined_xml: list[str] = []
        self.closed = False

    def defineXML(self, xml: str) -> FakeDomain:  # noqa: N802
        self.defined_xml.append(xml)
        domain = FakeDomain(self, xml)
        existing = self.domains.get(domain.domain_name)
        if existing is not None:
            domain.active = existing.active
        self.domains[domain.domain_name] = domain
        return domain

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802
        if name in self.domains:
            return self.domains[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)

    def listAllDomains(self, flags: int = 0) -> list[FakeDomain]:  # noqa: N802
        return list(self.domains.values())

    def storagePoolLookupByName(self, name: str) -> FakePool:  # noqa: N802
        if name in self.pools:
            return self.pools[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)

    def close(self) -> None:
        self.closed = True


_REFS = TlsCertRefs(
    client_cert_ref="remote/clientcert.pem",
    client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
    ca_cert_ref="remote/cacert.pem",
)

_BASE_VOLUME = "kdive-base-fedora-42.qcow2"
DOMAIN_NAME = f"kdive-{SYSTEM_ID}"


def _config(**overrides: Any) -> RemoteLibvirtConfig:
    values: dict[str, Any] = {
        "uri": "qemu+tls://host.example/system",
        "cert_refs": _REFS,
        "concurrent_allocation_cap": 2,
        "storage_pool": "default",
        "gdb_addr": "10.0.0.5",
        "gdb_port_min": 47000,
        "gdb_port_max": 47002,
        **overrides,
    }
    return RemoteLibvirtConfig(**values)


def _conn_with_base() -> FakeProvisionConn:
    pool = FakePool({_BASE_VOLUME: FakeVolume(_BASE_VOLUME)})
    return FakeProvisionConn({"default": pool})


def _ticker(step: float = 1.0) -> Callable[[], float]:
    now = {"t": 0.0}

    def _monotonic() -> float:
        current = now["t"]
        now["t"] += step
        return current

    return _monotonic


def _provisioner(
    conn: FakeProvisionConn,
    tmp_path: Path,
    config: RemoteLibvirtConfig | None = None,
    **overrides: Any,
) -> tuple[RemoteLibvirtProvision, list[str]]:
    opened: list[str] = []

    def _open(uri: str) -> FakeProvisionConn:
        opened.append(uri)
        return conn

    kwargs: dict[str, Any] = {
        "secret_registry": SecretRegistry(),
        "config_factory": lambda: config if config is not None else _config(),
        "open_connection": _open,
        "secret_backend_factory": RecordingBackend,
        "pki_base_dir": tmp_path,
        "sleep": lambda _s: None,
        "monotonic": _ticker(),
        **overrides,
    }
    return RemoteLibvirtProvision(**kwargs), opened


def test_provision_defines_starts_and_waits_for_agent(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)

    name = provisioner.provision(SYSTEM_ID, _remote_profile())

    assert name == DOMAIN_NAME
    domain = conn.domains[DOMAIN_NAME]
    assert domain.active
    assert recorded_gdb_port(domain.xml) == 47000
    overlay = overlay_volume_name(SYSTEM_ID)
    assert overlay in conn.pools["default"].volumes
    [volume_xml] = conn.pools["default"].created_xml
    assert f"/pool/{_BASE_VOLUME}" in volume_xml
    assert conn.closed


def test_provision_reuses_existing_overlay(tmp_path: Path) -> None:
    conn = _conn_with_base()
    pool = conn.pools["default"]
    overlay = overlay_volume_name(SYSTEM_ID)
    pool.volumes[overlay] = FakeVolume(overlay, pool=pool)
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.provision(SYSTEM_ID, _remote_profile())

    assert pool.created_xml == []


def test_provision_skips_ports_recorded_by_other_domains(tmp_path: Path) -> None:
    conn = _conn_with_base()
    other = render_domain_xml(
        UUID(int=1),
        _remote_profile(),
        pool="default",
        volume="other-overlay",
        gdb_addr="10.0.0.5",
        gdb_port=47000,
    )
    conn.defineXML(other)
    conn.defined_xml.clear()
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.provision(SYSTEM_ID, _remote_profile())

    assert recorded_gdb_port(conn.domains[DOMAIN_NAME].xml) == 47001


def test_provision_retry_reuses_own_recorded_port(tmp_path: Path) -> None:
    conn = _conn_with_base()
    own = render_domain_xml(
        SYSTEM_ID,
        _remote_profile(),
        pool="default",
        volume=overlay_volume_name(SYSTEM_ID),
        gdb_addr="10.0.0.5",
        gdb_port=47001,
    )
    conn.defineXML(own)
    conn.domains[DOMAIN_NAME].active = True
    conn.defined_xml.clear()
    # A retry against a running domain: create() reports already-running.
    conn.create_results = [libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)]
    provisioner, _ = _provisioner(conn, tmp_path)

    name = provisioner.provision(SYSTEM_ID, _remote_profile())

    assert name == DOMAIN_NAME
    assert recorded_gdb_port(conn.domains[DOMAIN_NAME].xml) == 47001


def test_provision_start_failure_advances_to_next_port(tmp_path: Path) -> None:
    conn = _conn_with_base()
    conn.create_results = [libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR), None]
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.provision(SYSTEM_ID, _remote_profile())

    assert recorded_gdb_port(conn.defined_xml[0]) == 47000
    assert recorded_gdb_port(conn.defined_xml[1]) == 47001
    assert recorded_gdb_port(conn.domains[DOMAIN_NAME].xml) == 47001


def test_provision_start_failures_exhaust_bounded_attempts(tmp_path: Path) -> None:
    conn = _conn_with_base()
    conn.create_results = [libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)] * 3
    provisioner, _ = _provisioner(conn, tmp_path)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert DOMAIN_NAME not in conn.domains  # transactional: undefined after failure
    # The overlay this attempt created is reclaimed.
    assert overlay_volume_name(SYSTEM_ID) not in conn.pools["default"].volumes


def test_provision_overlay_create_failure_is_provisioning_failure(tmp_path: Path) -> None:
    conn = _conn_with_base()
    conn.pools["default"].create_error = libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
    provisioner, _ = _provisioner(conn, tmp_path)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert conn.defined_xml == []


def test_provision_missing_base_volume_is_configuration_error(tmp_path: Path) -> None:
    conn = FakeProvisionConn()  # pool exists, base volume absent
    provisioner, _ = _provisioner(conn, tmp_path)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "base" in str(excinfo.value).lower()


def test_provision_missing_pool_is_configuration_error(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path, config=_config(storage_pool="absent-pool"))

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_provision_skips_domain_vanishing_during_enumeration(tmp_path: Path) -> None:
    conn = _conn_with_base()
    other = render_domain_xml(
        UUID(int=2),
        _remote_profile(),
        pool="default",
        volume="other-overlay",
        gdb_addr="10.0.0.5",
        gdb_port=47000,
    )
    conn.defineXML(other)
    conn.domains[f"kdive-{UUID(int=2)}"].xml_error = libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
    conn.defined_xml.clear()
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.provision(SYSTEM_ID, _remote_profile())

    # The vanished domain's port is treated as free (it is being released).
    assert recorded_gdb_port(conn.domains[DOMAIN_NAME].xml) == 47000


def test_provision_without_remote_section_opens_no_connection(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, opened = _provisioner(conn, tmp_path)
    local = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://example/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/f.qcow2"}
                }
            },
        }
    )

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, local)

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert opened == []


def test_provision_without_gdb_addr_opens_no_connection(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, opened = _provisioner(conn, tmp_path, config=_config(gdb_addr=None))

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_REMOTE_LIBVIRT_GDB_ADDR" in str(excinfo.value)
    assert opened == []


def test_provision_agent_timeout_leaves_domain_running(tmp_path: Path) -> None:
    conn = _conn_with_base()
    conn.agent_script[DOMAIN_NAME] = ["disconnected"] * 1000
    provisioner, _ = _provisioner(conn, tmp_path, monotonic=_ticker(100.0), agent_timeout_s=180.0)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert "agent" in str(excinfo.value).lower()
    domain = conn.domains[DOMAIN_NAME]  # left defined and running (diagnosable artifact)
    assert domain.active
    assert not domain.undefined
    # The overlay is left in place with the running domain.
    assert overlay_volume_name(SYSTEM_ID) in conn.pools["default"].volumes


def test_provision_domain_exit_during_agent_wait_fails_fast(tmp_path: Path) -> None:
    conn = _conn_with_base()
    conn.agent_script[DOMAIN_NAME] = ["disconnected"] * 1000
    conn.active_script[DOMAIN_NAME] = [True, False]
    ticks: list[float] = []

    def _monotonic() -> float:
        ticks.append(1.0 * len(ticks))
        return ticks[-1]

    provisioner, _ = _provisioner(conn, tmp_path, monotonic=_monotonic, agent_timeout_s=180.0)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.provision(SYSTEM_ID, _remote_profile())

    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert "exited" in str(excinfo.value).lower()
    assert len(ticks) < 10  # failed fast, not by burning the timeout
    assert DOMAIN_NAME in conn.domains  # left defined for diagnosis


def test_teardown_destroys_undefines_and_deletes_overlay(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)
    provisioner.provision(SYSTEM_ID, _remote_profile())
    domain = conn.domains[DOMAIN_NAME]

    provisioner.teardown(DOMAIN_NAME)

    assert domain.destroyed
    assert domain.undefined
    assert DOMAIN_NAME not in conn.domains
    assert overlay_volume_name(SYSTEM_ID) not in conn.pools["default"].volumes
    assert conn.pools["default"].deleted == [overlay_volume_name(SYSTEM_ID)]


def test_teardown_of_absent_domain_still_deletes_overlay(tmp_path: Path) -> None:
    conn = _conn_with_base()
    pool = conn.pools["default"]
    overlay = overlay_volume_name(SYSTEM_ID)
    pool.volumes[overlay] = FakeVolume(overlay, pool=pool)
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.teardown(DOMAIN_NAME)  # idempotent re-teardown

    assert overlay not in pool.volumes


def test_teardown_with_absent_overlay_is_noop_success(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)

    provisioner.teardown(DOMAIN_NAME)  # neither domain nor overlay exist

    assert conn.pools["default"].deleted == []


def test_teardown_reads_pool_from_domain_xml_on_config_drift(tmp_path: Path) -> None:
    # Provisioned into "old-pool"; config has since been repointed to "default".
    old_pool = FakePool({_BASE_VOLUME: FakeVolume(_BASE_VOLUME)})
    conn = FakeProvisionConn({"default": FakePool(), "old-pool": old_pool})
    provisioner, _ = _provisioner(conn, tmp_path, config=_config(storage_pool="old-pool"))
    provisioner.provision(SYSTEM_ID, _remote_profile())
    drifted, _ = _provisioner(conn, tmp_path, config=_config(storage_pool="default"))

    drifted.teardown(DOMAIN_NAME)

    assert overlay_volume_name(SYSTEM_ID) not in old_pool.volumes


def test_teardown_swallows_not_running_destroy(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)
    provisioner.provision(SYSTEM_ID, _remote_profile())
    domain = conn.domains[DOMAIN_NAME]

    domain.destroy_error = libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)

    provisioner.teardown(DOMAIN_NAME)

    assert DOMAIN_NAME not in conn.domains


def test_teardown_other_libvirt_error_is_infrastructure_failure(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)
    provisioner.provision(SYSTEM_ID, _remote_profile())
    domain = conn.domains[DOMAIN_NAME]

    domain.destroy_error = libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    with pytest.raises(CategorizedError) as excinfo:
        provisioner.teardown(DOMAIN_NAME)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_reprovision_wipes_then_provisions(tmp_path: Path) -> None:
    conn = _conn_with_base()
    provisioner, _ = _provisioner(conn, tmp_path)
    provisioner.provision(SYSTEM_ID, _remote_profile())
    first_overlay_creates = len(conn.pools["default"].created_xml)

    name = provisioner.reprovision(SYSTEM_ID, _remote_profile(crashkernel="512M"))

    assert name == DOMAIN_NAME
    assert conn.domains[DOMAIN_NAME].active
    # The old overlay was deleted and a fresh one created for the new profile.
    assert conn.pools["default"].deleted == [overlay_volume_name(SYSTEM_ID)]
    assert len(conn.pools["default"].created_xml) == first_overlay_creates + 1
