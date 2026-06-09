"""Artifact value and key-construction tests."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, artifact_key, owner_prefix


def test_artifact_key_and_owner_prefix_share_layout() -> None:
    assert artifact_key("local", "runs", "r1", "kernel") == "local/runs/r1/kernel"
    assert owner_prefix("local", "runs", "r1") == "local/runs/r1/"
    assert (
        ArtifactWriteRequest(
            tenant="local",
            owner_kind="runs",
            owner_id="r1",
            name="kernel",
            data=b"data",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        ).key()
        == "local/runs/r1/kernel"
    )


def test_artifact_key_rejects_invalid_component() -> None:
    with pytest.raises(CategorizedError) as exc:
        artifact_key("local", "runs", "bad/id", "kernel")

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
