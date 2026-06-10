"""Unimplemented remote-libvirt planes — buildable, fail-fast stubs (ADR-0076).

The M2 foundation lands the package, kind, transport, and discovery; provisioning
(``remote_libvirt.provisioning``, ADR-0080), build (``remote_libvirt.build``, ADR-0081),
install/boot (``remote_libvirt.install``, ADR-0082), and connect/debug + introspection
(``remote_libvirt.connect`` / ``.debug`` / ``.introspect``, ADR-0083) are real; the
control/retrieve plane lands in the later M2 issue. Until then each remaining plane raises a
typed ``MISSING_DEPENDENCY`` (the ports' documented category for an unavailable provider seam)
so the runtime is buildable — the ADR-0071 CHECK↔registry parity invariant — without pretending
the plane works.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.providers.ports import CaptureOutput, CrashOutput


def _unimplemented(plane: str) -> NoReturn:
    raise CategorizedError(
        f"remote-libvirt {plane} is not implemented yet (a later M2 change supplies it)",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"plane": plane},
    )


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
