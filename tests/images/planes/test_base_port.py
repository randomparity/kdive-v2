"""Contract tests for the `RootfsBuildPlane` port (M2.4/2, ADR-0092).

The port is the public contract issue #284 (remote plane) implements and inherits nothing from,
so these pin the dataclass field set and the `build(spec) -> RootfsBuildOutput` shape. A change
here is a breaking change to a downstream implementer.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kdive.images.planes.base import (
    RootfsBuildOutput,
    RootfsBuildPlane,
    RootfsBuildSpec,
)


def test_spec_fields_and_types() -> None:
    fields = {f.name: f.type for f in dataclasses.fields(RootfsBuildSpec)}
    assert set(fields) == {
        "provider",
        "name",
        "arch",
        "releasever",
        "packages",
        "source_image_digest",
        "capabilities",
    }
    spec = RootfsBuildSpec(
        provider="p",
        name="n",
        arch="x86_64",
        releasever="43",
        packages=("a",),
        source_image_digest="sha256:x",
        capabilities=("agent",),
    )
    assert dataclasses.is_dataclass(spec)


def _set_attr(target: object, attr: str, value: object) -> None:
    """Assign through an ``object``-typed boundary so the frozen-write is a runtime check."""
    target.__setattr__(attr, value)


def test_spec_is_frozen() -> None:
    spec = RootfsBuildSpec(
        provider="p",
        name="n",
        arch="x86_64",
        releasever="43",
        packages=(),
        source_image_digest="sha256:x",
        capabilities=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        _set_attr(spec, "name", "other")


def test_output_fields() -> None:
    fields = {f.name for f in dataclasses.fields(RootfsBuildOutput)}
    assert fields == {"qcow2_path", "digest", "provenance"}
    out = RootfsBuildOutput(qcow2_path=Path("/x.qcow2"), digest="sha256:y", provenance={"k": 1})
    assert isinstance(out.qcow2_path, Path)
    assert out.digest == "sha256:y"
    assert out.provenance == {"k": 1}


def test_plane_is_runtime_checkable_protocol() -> None:
    class _Impl:
        def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
            return RootfsBuildOutput(qcow2_path=Path("/x"), digest="sha256:z", provenance={})

    assert isinstance(_Impl(), RootfsBuildPlane)

    class _NotAPlane:
        pass

    assert not isinstance(_NotAPlane(), RootfsBuildPlane)
