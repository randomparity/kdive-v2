"""Tests for the local-libvirt Provisioning plane (ADR-0025)."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import libvirt
import pytest
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt import discovery
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    domain_name_for,
    render_domain_xml,
    validate_profile,
)
from tests.providers.local_libvirt.conftest import libvirt_error

_SYS = UUID("11111111-1111-1111-1111-111111111111")

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
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            "crashkernel": "256M",
        }
    },
}


def _profile(**overrides: Any) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID)
    data["provider"]["local-libvirt"].update(overrides)
    return ProvisioningProfile.parse(data)


def test_domain_name_is_kdive_prefixed() -> None:
    assert domain_name_for(_SYS) == "kdive-11111111-1111-1111-1111-111111111111"


def test_render_carries_name_memory_vcpu_machine_and_rootfs() -> None:
    root = _safe_fromstring(render_domain_xml(_SYS, _profile()))
    assert root.findtext("name") == "kdive-11111111-1111-1111-1111-111111111111"
    assert root.findtext("memory") == "4096"
    assert root.findtext("vcpu") == "4"
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "x86_64"
    assert os_type.get("machine") == "pc-q35-9.0"
    source = root.find("devices/disk/source")
    assert source is not None
    assert source.get("file") == "oci://registry.internal/rootfs/fedora-40@sha256:abc123"


def test_render_has_no_kernel_or_cmdline() -> None:
    # The kdump crashkernel reservation is the install/boot plane's job (#17), not provision's.
    root = _safe_fromstring(render_domain_xml(_SYS, _profile()))
    assert root.find("os/kernel") is None
    assert root.find("os/cmdline") is None


def test_render_metadata_tag_round_trips_through_discovery() -> None:
    root = _safe_fromstring(render_domain_xml(_SYS, _profile()))
    tag = root.find(f"metadata/{{{discovery._KDIVE_METADATA_NS}}}system")
    assert tag is not None
    assert discovery._parse_system_id(ET.tostring(tag, encoding="unicode")) == str(_SYS)


def test_render_defaults_machine_when_absent() -> None:
    root = _safe_fromstring(render_domain_xml(_SYS, _profile(domain_xml_params={})))
    os_type = root.find("os/type")
    assert os_type is not None and os_type.get("machine") == "q35"


def test_validate_profile_rejects_unknown_domain_xml_param() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_profile(_profile(domain_xml_params={"machine": "q35", "bogus": "x"}))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_render_rejects_unknown_domain_xml_param() -> None:
    # render re-checks at the worker boundary (a hand-built jsonb that bypassed the tool).
    with pytest.raises(CategorizedError):
        render_domain_xml(_SYS, _profile(domain_xml_params={"nope": "x"}))


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

    def defineXML(self, xml: str) -> _ProvDomain:
        if self.define_error is not None:
            raise libvirt_error(self.define_error)
        name = _safe_fromstring(xml).findtext("name")
        assert name is not None
        return self.defined.setdefault(name, _ProvDomain(name))

    def lookupByName(self, name: str) -> _ProvDomain:
        if self.lookup_error is not None:
            raise libvirt_error(self.lookup_error)
        if name not in self.defined:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.defined[name]


def _prov(conn: _ProvConn) -> LocalLibvirtProvisioning:
    return LocalLibvirtProvisioning(connect=lambda: conn)


def test_provision_defines_and_starts_returns_name() -> None:
    conn = _ProvConn()
    name = _prov(conn).provision(_SYS, _profile())
    assert name == "kdive-11111111-1111-1111-1111-111111111111"
    assert conn.defined[name].created is True


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


def test_teardown_destroys_and_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.destroyed is True and dom.undefined is True


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


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    prov = LocalLibvirtProvisioning.from_env()  # building must not open a connection
    assert isinstance(prov, LocalLibvirtProvisioning)
