"""Adversarial: XML handling across the local-libvirt provider planes.

Two surfaces:
  * **Construction** — `render_domain_xml` (provisioning) and the install plane's
    direct-kernel `<os>` rewrite build XML with `ElementTree`, so a hostile profile or
    cmdline value must round-trip as data, never inject elements/attributes.
  * **Parsing of libvirtd output** — `discovery` treats libvirtd-emitted XML
    (capabilities, domain metadata) as a *trust boundary* and parses it with
    `defusedxml`, neutralizing entity-expansion (billion-laughs). The install plane
    reads the same source (`domain.XMLDesc()`); it must defend it the same way.

The install-plane entity-expansion test is the falsifying case for Finding (XXE) — it
fails until install parses `XMLDesc` with `defusedxml`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

import pytest
from defusedxml.common import DefusedXmlException
from hypothesis import given
from hypothesis import strategies as st

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.discovery import _parse_arch, _parse_system_id
from kdive.providers.local_libvirt.install import LocalLibvirtInstall, ReadinessResult
from kdive.providers.local_libvirt.provisioning import render_domain_xml
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn

_SYS = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")

# A DOCTYPE + nested-entity document: the seed of a billion-laughs expansion. stdlib
# ElementTree expands it; defusedxml refuses it.
_ENTITY_BOMB_OS = """<?xml version="1.0"?>
<!DOCTYPE domain [
  <!ENTITY a "AAAAAAAAAA">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<domain type="kvm"><name>kdive-11111111-1111-1111-1111-111111111111</name>
<memory unit="MiB">2048</memory><vcpu>2</vcpu>
<os><type arch="x86_64" machine="q35">hvm</type></os>
<devices/><cmdline>&b;</cmdline></domain>"""


def _profile(
    *, arch: str = "x86_64", rootfs: str = "/var/lib/kdive/rootfs.qcow2"
) -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": arch,
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "domain_xml_params": {},
                    "rootfs": {"kind": "local", "path": rootfs},
                    "crashkernel": "256M",
                }
            },
        }
    )


# --- construction: hostile values are data, not markup -------------------------------

_hostile = st.sampled_from(
    [
        'x"/></source><evil for="injection"/><source file="',
        "</domain><domain><name>pwned</name>",
        "a & b < c > d",
        "'; <!ENTITY x SYSTEM 'file:///etc/passwd'>",
        "quote\"and'apos",
        "normal-value",
    ]
)


@given(rootfs=_hostile)
def test_render_domain_xml_never_lets_a_profile_value_inject_markup(rootfs: str) -> None:
    rootfs_path = f"/var/lib/kdive/{rootfs}"
    xml = render_domain_xml(_SYS, _profile(), disk_path=rootfs_path)
    root = ET.fromstring(xml)  # noqa: S314 - self-rendered, asserting structure
    # Exactly one disk source, and its file attr equals the hostile value verbatim — the
    # value crossed as an attribute, creating no new elements.
    sources = root.findall("./devices/disk/source")
    assert len(sources) == 1
    assert sources[0].get("file") == rootfs_path
    assert root.find("evil") is None and root.tag == "domain"


def test_console_log_element_does_not_enable_append() -> None:
    # Readiness pre-marker scoping (ADR-0055 §3/§8) relies on the console log being
    # truncated per create(). QEMU/libvirt default logappend=off; the rendered <log> must
    # not set append='on', or a stale prior-boot marker could survive into the next boot.
    domain = ET.fromstring(  # noqa: S314 - self-rendered
        render_domain_xml(_SYS, _profile(), disk_path="/var/lib/kdive/rootfs/base.qcow2")
    )
    logs = domain.findall("./devices/serial/log")
    assert logs, "the always-on serial console <log> tee must be present (ADR-0049 §4)"
    for log in logs:
        assert log.get("append") != "on"


# --- parsing libvirtd output: install must defend XMLDesc like discovery does --------


def _installer(conn: FakeLibvirtConn, staging_root: Path) -> LocalLibvirtInstall:
    return LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=lambda ref, dest: dest.write_bytes(b"k"),
        fetch_initrd=lambda ref, dest: dest.write_bytes(b"i"),
        readiness=lambda system_id: ReadinessResult(answered=True, ok=True),
        staging_root=staging_root,
        boot_window_polls=3,
    )


def test_install_rejects_entity_expansion_in_domain_xmldesc(tmp_path: Path) -> None:
    # A domain whose XMLDesc carries a DOCTYPE + entities must be refused with a clean
    # install_failure — never silently entity-expanded the way stdlib ElementTree does.
    domain = FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS), xml_desc=_ENTITY_BOMB_OS)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _installer(conn, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        inst.install(_SYS, _RUN, "kref", cmdline="console=ttyS0")
    assert exc.value.category is ErrorCategory.INSTALL_FAILURE
    # And it never reached defineXML with an expanded document.
    assert conn.defined_xml == []


def test_install_still_accepts_a_benign_xmldesc(tmp_path: Path) -> None:
    # Guard against over-rejecting: a normal libvirt XMLDesc (no DOCTYPE) still installs.
    domain = FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS))  # default benign XML
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _installer(conn, tmp_path)
    inst.install(_SYS, _RUN, "kref", cmdline="console=ttyS0 crashkernel=256M")
    assert len(conn.defined_xml) == 1
    os_el = ET.fromstring(conn.defined_xml[0]).find("os")  # noqa: S314 - self-rendered
    assert os_el is not None and os_el.find("kernel") is not None


# --- discovery: the documented trust-boundary parse contract -------------------------

_CAPS_BOMB = """<?xml version="1.0"?>
<!DOCTYPE capabilities [ <!ENTITY a "AAAA"> <!ENTITY b "&a;&a;&a;&a;&a;"> ]>
<capabilities><host><cpu><arch>&b;</arch></cpu></host></capabilities>"""

_META_BOMB = """<?xml version="1.0"?>
<!DOCTYPE kdive [ <!ENTITY a "AAAA"> <!ENTITY b "&a;&a;&a;&a;&a;"> ]>
<kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">&b;</kdive:system>"""


def test_parse_arch_reads_valid_caps_and_unknowns_malformed() -> None:
    valid = "<capabilities><host><cpu><arch>aarch64</arch></cpu></host></capabilities>"
    assert _parse_arch(valid) == "aarch64"
    assert _parse_arch("<capabilities><host/></capabilities>") == "unknown"  # arch absent
    assert _parse_arch("<not-closed") == "unknown"  # malformed → unknown, not a crash


def test_parse_arch_refuses_entity_expansion_fail_loud() -> None:
    # The docstring promises an *attack* document raises (fail loud), not silent "unknown".
    with pytest.raises(DefusedXmlException):
        _parse_arch(_CAPS_BOMB)


def test_parse_system_id_reads_valid_and_none_on_malformed() -> None:
    meta = '<kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">abc-123</kdive:system>'
    assert _parse_system_id(meta) == "abc-123"
    empty = '<kdive:system xmlns:kdive="https://kdive.dev/libvirt/1"></kdive:system>'
    assert _parse_system_id(empty) is None
    assert _parse_system_id("<broken") is None


def test_parse_system_id_refuses_entity_expansion_fail_loud() -> None:
    with pytest.raises(DefusedXmlException):
        _parse_system_id(_META_BOMB)
