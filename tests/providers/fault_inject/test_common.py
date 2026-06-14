"""Shared fault-inject provider constants."""

from __future__ import annotations

from kdive.providers.fault_inject import _common


def test_common_constants_are_stable_artifact_inputs() -> None:
    assert _common.TENANT == "fault-inject"
    assert _common.SYNTHETIC_BUILD_ID.startswith("fa017")
    assert len(_common.SYNTHETIC_BUILD_ID) == 40
