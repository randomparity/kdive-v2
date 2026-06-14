"""Parse-time validation tests for the systems.toml v2 model (issue #389, Task 1.2)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kdive.inventory.errors import InventoryError
from kdive.inventory.model import (
    BuildSource,
    InventoryDoc,
    S3Source,
    StagedSource,
)


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 2,
        "image": [
            {
                "provider": "remote-libvirt",
                "name": "base",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": "staged", "volume": "base.qcow2"},
            }
        ],
        "remote_libvirt": [
            {
                "name": "h1",
                "uri": "qemu+tls://h1/system",
                "gdb_addr": "10.0.0.1",
                "gdbstub_range": "47000:47099",
                "client_cert_ref": "c.pem",
                "client_key_ref": "k.pem",  # pragma: allowlist secret - filename ref
                "ca_cert_ref": "ca.pem",  # pragma: allowlist secret - filename ref
                "base_image": "base",
                "cost_class": "remote",
                "concurrent_allocation_cap": 1,
                "shapes": ["small"],
            }
        ],
    }
    base.update(overrides)
    return base


def test_wellformed_parses() -> None:
    doc = InventoryDoc.parse(_doc())
    src = doc.image[0].source
    assert isinstance(src, StagedSource)
    assert src.volume == "base.qcow2"
    assert doc.remote_libvirt[0].base_image == "base"


def test_empty_document_parses() -> None:
    doc = InventoryDoc.parse({"schema_version": 2})
    assert doc.image == []
    assert doc.remote_libvirt == []
    assert doc.local_libvirt == []
    assert doc.fault_inject == []
    assert doc.build_host == []


def test_image_identity_property() -> None:
    doc = InventoryDoc.parse(_doc())
    assert doc.image[0].identity == ("remote-libvirt", "base", "x86_64")


def test_s3_source_with_digest() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "i",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {
                    "kind": "s3",
                    "object_key": "k",
                    "digest": "sha256:ab",
                },
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, S3Source)
    assert src.object_key == "k"
    assert src.digest == "sha256:ab"


def test_s3_source_digest_optional() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "i",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": "s3", "object_key": "k"},
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, S3Source)
    assert src.digest is None


def test_build_source() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "built",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {
                    "kind": "build",
                    "base": "fedora-43",
                    "components": ["kdump"],
                },
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, BuildSource)
    assert src.base == "fedora-43"
    assert src.components == ["kdump"]


def test_duplicate_image_identity_rejected() -> None:
    img = {
        "provider": "local-libvirt",
        "name": "dup",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(image=[img, dict(img)], remote_libvirt=[]))


def test_same_name_different_arch_is_not_duplicate() -> None:
    # identity is (provider, name, arch); a different arch is a distinct image.
    base = {
        "provider": "local-libvirt",
        "name": "dup",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    d = _doc(
        image=[
            {**base, "arch": "x86_64"},
            {**base, "arch": "aarch64"},
        ],
        remote_libvirt=[],
    )
    doc = InventoryDoc.parse(d)
    assert len(doc.image) == 2


def test_base_image_cross_ref_must_name_declared_image() -> None:
    d = _doc()
    d["remote_libvirt"][0]["base_image"] = "does-not-exist"
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_unknown_source_kind_rejected() -> None:
    d = _doc()
    d["image"][0]["source"] = {"kind": "ftp", "url": "x"}
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_wrong_schema_version_rejected() -> None:
    d = _doc(schema_version=1)
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_duplicate_remote_instance_name_rejected() -> None:
    d = _doc()
    second = dict(d["remote_libvirt"][0])
    d["remote_libvirt"] = [d["remote_libvirt"][0], second]
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_multiple_remote_instances_rejected_until_per_op_selection_is_wired() -> None:
    d = _doc()
    second = {**d["remote_libvirt"][0], "name": "h2", "uri": "qemu+tls://h2/system"}
    d["remote_libvirt"] = [d["remote_libvirt"][0], second]
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    assert excinfo.value.entry == "remote_libvirt"
    assert excinfo.value.field == "instances"
    assert "multiple instances are not supported" in str(excinfo.value)


def test_duplicate_fault_inject_name_rejected() -> None:
    inst = {
        "name": "fi",
        "cost_class": "local",
        "vcpus": 2,
        "memory_mb": 1024,
    }
    d = _doc(remote_libvirt=[], fault_inject=[inst, dict(inst)])
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_local_libvirt_instance_parses() -> None:
    d = _doc(
        remote_libvirt=[],
        local_libvirt=[
            {
                "name": "loc",
                "cost_class": "local",
                "host_uri": "qemu:///system",
            }
        ],
    )
    doc = InventoryDoc.parse(d)
    assert doc.local_libvirt[0].host_uri == "qemu:///system"


def test_build_host_instance_parses() -> None:
    d = _doc(
        remote_libvirt=[],
        build_host=[
            {
                "name": "bh",
                "kind": "ssh",
                "workspace_root": "/srv/build",
                "max_concurrent": 2,
            }
        ],
    )
    doc = InventoryDoc.parse(d)
    assert doc.build_host[0].workspace_root == "/srv/build"
    assert doc.build_host[0].max_concurrent == 2
    assert doc.build_host[0].base_image_volume is None


def test_missing_required_field_rejected() -> None:
    d = _doc()
    del d["image"][0]["root_device"]
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


_SOURCE_STRATEGY = st.one_of(
    st.fixed_dictionaries(
        {
            "kind": st.just("staged"),
            "volume": st.text(min_size=1, max_size=20),
        }
    ),
    st.fixed_dictionaries(
        {
            "kind": st.just("s3"),
            "object_key": st.text(min_size=1, max_size=20),
        }
    ),
    st.fixed_dictionaries(
        {
            "kind": st.just("build"),
            "base": st.text(min_size=1, max_size=20),
        }
    ),
)


@given(source=_SOURCE_STRATEGY)
def test_source_union_discriminates_on_kind(source: dict[str, Any]) -> None:
    d = _doc(
        image=[
            {
                "provider": "p",
                "name": "n",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": source,
            }
        ],
        remote_libvirt=[],
    )
    doc = InventoryDoc.parse(d)
    assert doc.image[0].source.kind == source["kind"]


@given(kind=st.text(min_size=1, max_size=8).filter(lambda k: k not in {"s3", "build", "staged"}))
def test_unknown_discriminator_always_raises_inventory_error(kind: str) -> None:
    d = _doc(
        image=[
            {
                "provider": "p",
                "name": "n",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": kind, "x": "y"},
            }
        ],
        remote_libvirt=[],
    )
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_cross_ref_error_preserves_precise_entry_and_field() -> None:
    # A semantic failure must surface its precise entry/field, not be flattened to
    # the generic ('inventory', 'schema') locator a pydantic after-validator would force.
    d = _doc()
    d["remote_libvirt"][0]["base_image"] = "nope"
    try:
        InventoryDoc.parse(d)
    except InventoryError as exc:
        assert exc.entry == "remote_libvirt[h1]"
        assert exc.field == "base_image"
        assert "nope" in str(exc)
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")


def test_duplicate_identity_error_preserves_precise_entry_and_field() -> None:
    img = {
        "provider": "local-libvirt",
        "name": "dup",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    try:
        InventoryDoc.parse(_doc(image=[img, dict(img)], remote_libvirt=[]))
    except InventoryError as exc:
        assert exc.entry == "image[dup]"
        assert exc.field == "identity"
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")


def test_duplicate_instance_name_error_preserves_kind_and_field() -> None:
    inst = {"name": "fi", "cost_class": "local", "vcpus": 2, "memory_mb": 1024}
    try:
        InventoryDoc.parse(_doc(remote_libvirt=[], fault_inject=[inst, dict(inst)]))
    except InventoryError as exc:
        assert exc.entry == "fault_inject"
        assert exc.field == "name"
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")
