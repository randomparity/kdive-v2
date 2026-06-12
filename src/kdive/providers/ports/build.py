"""Build provider contracts."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_results import BuildOutput


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
