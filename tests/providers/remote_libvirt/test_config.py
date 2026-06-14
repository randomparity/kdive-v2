"""Inventory-backed config for the remote-libvirt provider (ADR-0076, ADR-0077, ADR-0112).

Phase 3 (#395) deletes the ``KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR}`` singletons; the
remote connection config is now resolved per op from the ``systems.toml`` ``[[remote_libvirt]]``
instance. The libvirt storage-pool / network / machine knobs stay operational env settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    is_remote_libvirt_configured,
    remote_config_from_inventory,
)

_INSTANCE = """
name = "ub24-big"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "remote/clientcert.pem"
client_key_ref = "remote/clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote/cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
"""

_IMAGE = """
[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"
"""


def _write_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    instances: str = _INSTANCE,
    image: str = _IMAGE,
) -> Path:
    blocks = "".join(f"[[remote_libvirt]]{block}" for block in instances.split("---") if block)
    doc = f"schema_version = 2\n{image}\n{blocks}\n"
    path = tmp_path / "systems.toml"
    path.write_text(doc)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    return path


def _no_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    config.load()


def test_single_instance_builds_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.uri == "qemu+tls://host.example/system"
    assert cfg.cert_refs.client_cert_ref == "remote/clientcert.pem"
    assert cfg.cert_refs.client_key_ref == "remote/clientkey.pem"  # pragma: allowlist secret
    assert cfg.cert_refs.ca_cert_ref == "remote/cacert.pem"
    assert cfg.gdb_addr == "192.168.10.20"
    assert cfg.gdb_port_min == 47000
    assert cfg.gdb_port_max == 47099
    assert cfg.concurrent_allocation_cap == 1  # model default


def test_configured_detection_tracks_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    assert not is_remote_libvirt_configured()
    _write_inventory(tmp_path, monkeypatch)
    assert is_remote_libvirt_configured()


def test_no_instance_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_multiple_instances_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    second = _INSTANCE.replace('name = "ub24-big"', 'name = "ub24-small"')
    _write_inventory(tmp_path, monkeypatch, instances=f"{_INSTANCE}---{second}")
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "multiple" in str(excinfo.value)


def test_malformed_inventory_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n[[remote_libvirt]\n")  # malformed table header
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_uri_with_no_verify_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _INSTANCE.replace(
        "qemu+tls://host.example/system", "qemu+tls://host.example/system?no_verify=1"
    )
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_explicit_cap_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = f"{_INSTANCE}concurrent_allocation_cap = 4\n"
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    assert remote_config_from_inventory().concurrent_allocation_cap == 4


def test_provisioning_knob_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.storage_pool == "default"
    assert cfg.network == "default"
    assert cfg.machine == "pc"


def test_provisioning_knobs_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_STORAGE_POOL", "kdive-pool")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_NETWORK", "lab-net")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_MACHINE", "q35")
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.storage_pool == "kdive-pool"
    assert cfg.network == "lab-net"
    assert cfg.machine == "q35"


@pytest.mark.parametrize("bad", ["low:47099", "47000", "0:47099", "47099:47000"])
def test_bad_gdbstub_range_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', f'gdbstub_range = "{bad}"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
