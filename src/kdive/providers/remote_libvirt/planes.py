"""Unimplemented remote-libvirt planes — buildable, fail-fast stubs (ADR-0076).

The M2 foundation lands the package, kind, transport, and discovery; provisioning,
the artifact seam, build, install, connect/debug, and control/retrieve land in the
later M2 issues. Until then each plane raises a typed ``MISSING_DEPENDENCY`` (the
ports' documented category for an unavailable provider seam) so the runtime is
buildable — the ADR-0071 CHECK↔registry parity invariant — without pretending the
plane works.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.profiles.build import ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports import (
    BuildOutput,
    CaptureOutput,
    CrashOutput,
    InstallRequest,
    IntrospectOutput,
    SystemHandle,
    TransportHandle,
)


def _unimplemented(plane: str) -> NoReturn:
    raise CategorizedError(
        f"remote-libvirt {plane} is not implemented yet (a later M2 change supplies it)",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"plane": plane},
    )


class UnimplementedProvisioner:
    """Provisioner port stub (lands with the remote provisioning issue)."""

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        _unimplemented("provisioning")

    def teardown(self, domain_name: str) -> None:
        _unimplemented("provisioning")

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        _unimplemented("provisioning")


class UnimplementedBuilder:
    """Builder port stub (lands with the remote build issue)."""

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        _unimplemented("build")


class UnimplementedInstaller:
    """Installer + Booter port stub (lands with the remote install issue)."""

    def install(self, request: InstallRequest) -> None:
        _unimplemented("install")

    def boot(self, system_id: UUID) -> None:
        _unimplemented("boot")


class UnimplementedConnector:
    """Connector port stub (lands with the remote connect/debug issue)."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        _unimplemented("connect")

    def close_transport(self, handle: TransportHandle) -> None:
        _unimplemented("connect")


class UnimplementedController:
    """Controller port stub (lands with the remote control/retrieve issue)."""

    def power(self, domain_name: str, action: PowerAction) -> None:
        _unimplemented("control")

    def force_crash(self, domain_name: str) -> None:
        _unimplemented("control")


class UnimplementedRetriever:
    """Retriever + CrashPostmortem port stub (lands with the remote retrieve issue)."""

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        _unimplemented("capture")

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        _unimplemented("crash postmortem")


class UnimplementedIntrospector:
    """Vmcore + live introspection port stub (lands with the remote debug issue)."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        _unimplemented("vmcore introspection")

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        _unimplemented("live introspection")
