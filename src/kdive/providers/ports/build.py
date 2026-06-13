"""Build provider contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.build_host.transport import BuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry


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


@runtime_checkable
class TransportCapableBuilder(Builder, Protocol):
    """A :class:`Builder` that can rebind its build onto a remote :class:`BuildTransport`.

    A remote build host (ssh or ephemeral_libvirt) runs the build over a transport rather than
    in the worker process; a builder advertises that it supports this by exposing
    ``over_transport``. The BUILD handler checks for this capability (not a concrete provider
    type), so any provider whose builder implements it can build on a remote host (ADR-0101).
    """

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> Builder:
        """Return a sibling builder whose every build step runs on ``transport``'s host."""
        ...
