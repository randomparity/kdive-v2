"""Loader fault-isolation tests for systems.toml (issue #389, Task 1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory, load_inventory_optional

GOOD = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
"""

BAD_TOML = "schema_version = 2\n[[image]\n"  # malformed table header

BAD_SCHEMA = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "ftp"
url = "x"
"""


def test_load_good(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory(p)
    assert doc.image[0].name == "base"


def test_malformed_toml_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_schema_failure_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_SCHEMA)
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_missing_file_raises_inventory_error(tmp_path: Path) -> None:
    # An explicitly-named path that is absent IS an error.
    with pytest.raises(InventoryError):
        load_inventory(tmp_path / "absent.toml")


def test_non_utf8_file_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_bytes(b"\xff\xfe schema_version = 2\n")
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_load_optional_returns_none_for_absent_path(tmp_path: Path) -> None:
    # The DEFAULT-path case: an absent file means "nothing declared", not an error.
    assert load_inventory_optional(tmp_path / "absent.toml") is None


def test_load_optional_parses_present_good_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory_optional(p)
    assert doc is not None
    assert doc.image[0].name == "base"


def test_load_optional_still_raises_on_present_malformed_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError):
        load_inventory_optional(p)
