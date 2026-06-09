from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    ComponentUploadRef,
    LocalComponentRef,
    parse_component_ref,
)


def test_parse_local_ref_requires_absolute_path() -> None:
    ref = parse_component_ref(
        {
            "kind": "local",
            "path": "/var/lib/kdive/rootfs/base.qcow2",
            "sha256": "sha256:" + "0" * 64,
        }
    )

    assert isinstance(ref, LocalComponentRef)
    assert ref.path == "/var/lib/kdive/rootfs/base.qcow2"


def test_parse_artifact_ref() -> None:
    ref = parse_component_ref(
        {"kind": "artifact", "artifact_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert isinstance(ref, ArtifactComponentRef)


def test_parse_component_upload_ref() -> None:
    ref = parse_component_ref(
        {"kind": "component-upload", "upload_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert isinstance(ref, ComponentUploadRef)


def test_parse_catalog_ref() -> None:
    ref = parse_component_ref({"kind": "catalog", "provider": "local-libvirt", "name": "fedora"})

    assert isinstance(ref, CatalogComponentRef)
    assert ref.provider == "local-libvirt"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "local", "path": "relative.img"},
        {"kind": "local", "path": "/x", "sha256": "deadbeef"},
        {"kind": "artifact", "artifact_id": "not-a-uuid"},
        {"kind": "component-upload", "upload_id": "not-a-uuid"},
        {"kind": "catalog", "provider": "remote-libvirt", "name": ""},
        {"kind": "url", "url": "https://example.invalid/x.qcow2"},
    ],
)
def test_parse_component_ref_maps_invalid_payloads_to_config_error(
    payload: dict[str, object],
) -> None:
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref(payload)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
