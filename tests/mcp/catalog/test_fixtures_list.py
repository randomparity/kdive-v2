"""``fixtures.list`` — provider-organized rootfs catalog read (#252, ADR-0089 §6).

A plain authenticated read (no platform gate, no per-tool audit): the fixture catalog is
the provider-organized rootfs inventory, not secret content. Coverage:

* it flattens each provider's rootfs entries into ``{provider, name, arch}`` rows;
* an empty catalog yields an empty list (no crash);
* the real source-tree default catalog loads and surfaces its ``local-libvirt`` rootfs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kdive.mcp.tools.catalog import fixtures
from kdive.provider_components.catalog import (
    FixtureCatalog,
    FixtureManifest,
    FixtureStorage,
    RootfsCatalogEntry,
)
from kdive.provider_components.references import LocalComponentRef
from tests.mcp.json_data import data_sequence, json_mapping


def _entry(provider: str, name: str) -> RootfsCatalogEntry:
    return RootfsCatalogEntry(
        provider=provider,
        name=name,
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        source=LocalComponentRef(kind="local", path=f"/var/lib/kdive/rootfs/{name}.qcow2"),
        visibility="public",
        capabilities=["console"],
    )


def _catalog(*entries: RootfsCatalogEntry) -> FixtureCatalog:
    manifest = FixtureManifest(
        schema_version=1,
        provider="local-libvirt",
        storage=FixtureStorage(
            allowed_component_roots=[],
            cache_dir=Path("/tmp/cache"),
            overlay_dir=Path("/tmp/overlays"),
        ),
    )
    return FixtureCatalog(manifest=manifest, rootfs=list(entries), profiles=[])


def test_lists_rootfs_catalog_entries(monkeypatch) -> None:
    catalog = _catalog(_entry("local-libvirt", "base"), _entry("local-libvirt", "cloud"))
    monkeypatch.setattr(fixtures, "load_fixture_catalog", lambda path=None: catalog)
    resp = asyncio.run(fixtures.list_fixtures_tool())
    assert resp.status == "ok"
    rows = [json_mapping(row) for row in data_sequence(resp, "fixtures")]
    names = {row["name"] for row in rows}
    assert {"base", "cloud"} <= names
    assert all(row["provider"] == "local-libvirt" for row in rows)


def test_empty_catalog_yields_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(fixtures, "load_fixture_catalog", lambda path=None: _catalog())
    resp = asyncio.run(fixtures.list_fixtures_tool())
    assert resp.status == "ok"
    assert data_sequence(resp, "fixtures") == []


def test_real_baseline_catalog_loads_local_libvirt_rootfs() -> None:
    # The tool reads the packaged seed_data baseline (ADR-0092 relocation); it still surfaces
    # the local-libvirt baseline rootfs inventory.
    resp = asyncio.run(fixtures.list_fixtures_tool())
    assert resp.status == "ok"
    rows = [json_mapping(row) for row in data_sequence(resp, "fixtures")]
    local = [row for row in rows if row["provider"] == "local-libvirt"]
    assert local, "packaged baseline catalog should expose local-libvirt rootfs entries"
