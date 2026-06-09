"""Remote-libvirt provisioning over the injected TLS connection (ADR-0080)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from defusedxml.ElementTree import fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.provisioning import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    allocate_gdb_port,
    overlay_volume_name,
    recorded_gdb_port,
    render_domain_xml,
    render_volume_xml,
)

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
