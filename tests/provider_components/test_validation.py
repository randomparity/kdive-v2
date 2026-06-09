from __future__ import annotations

from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.references import ArtifactComponentRef, LocalComponentRef
from kdive.provider_components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)


def test_accepts_supported_component_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={"rootfs": frozenset({"local"})},
    )

    reject_unsupported_component_source(
        caps,
        component_kind="rootfs",
        ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
    )


def test_rejects_remote_provider_local_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="remote-libvirt",
        accepted_component_sources={"rootfs": frozenset({"artifact", "catalog"})},
    )

    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind="rootfs",
            ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "path" not in caught.value.details


def test_rejects_unimplemented_local_libvirt_kernel_artifact_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={"kernel": frozenset({"local"})},
    )

    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind="kernel",
            ref=ArtifactComponentRef(
                kind="artifact",
                artifact_id=UUID("00000000-0000-0000-0000-000000000000"),
            ),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
