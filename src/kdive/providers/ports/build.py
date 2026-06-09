"""Build provider contracts."""

from __future__ import annotations

from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.components.artifacts import HeadResult
from kdive.profiles.build import ServerBuildProfile


class BuildOutput(NamedTuple):
    kernel_ref: str
    debuginfo_ref: str
    build_id: str


class ValidatedUpload(NamedTuple):
    output: BuildOutput
    heads: dict[str, HeadResult]


class Builder(Protocol):
    """Build port returning stored kernel and debuginfo refs."""

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store its boot artifact plus debuginfo.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for unresolvable refs or malformed
                build input, ``MISSING_DEPENDENCY`` for absent build tools or source roots,
                ``INFRASTRUCTURE_FAILURE`` for workspace/store IO failures, or
                ``BUILD_FAILURE`` for compiler/validation failures.
        """
        ...
