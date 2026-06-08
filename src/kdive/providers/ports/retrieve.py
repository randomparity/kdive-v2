"""Retrieve, crash-postmortem, and introspection provider contracts."""

from __future__ import annotations

from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.store.objectstore import StoredArtifact


class CaptureOutput(NamedTuple):
    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str


class CrashResult(NamedTuple):
    exit_status: int
    stdout: bytes
    stderr: bytes


class CrashOutput(NamedTuple):
    results: dict[str, object]
    transcript: str
    truncated: bool


class IntrospectOutput(NamedTuple):
    tasks: dict[str, object]
    modules: dict[str, object]
    sysinfo: dict[str, object]
    truncated: bool


class Retriever(Protocol):
    """Capture port keyed on the System id."""

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture, store, and redact a vmcore.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for build-id provenance mismatch or
                invalid capture input, ``MISSING_DEPENDENCY`` for unavailable provider/store
                seams, or ``READINESS_FAILURE`` when no complete vmcore is available.
        """
        ...


class CrashPostmortem(Protocol):
    """Crash postmortem port for command batches over a captured vmcore."""

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Run a bounded crash-command batch over a captured vmcore.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed refs, command batches, or
                build-id mismatch, ``MISSING_DEPENDENCY`` for unavailable object-store or
                crash seams, or ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        ...


class VmcoreIntrospector(Protocol):
    """Offline introspection port for captured vmcores."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Inspect a captured vmcore through the provider's offline helper.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed refs or build-id
                mismatch, ``MISSING_DEPENDENCY`` for unavailable object-store or helper
                seams, or ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        ...


class LiveIntrospector(Protocol):
    """Live introspection port over an existing transport handle."""

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        """Inspect a live guest through an existing debug transport.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed handles or unknown
                helpers, ``MISSING_DEPENDENCY`` for unavailable helper seams, or
                ``DEBUG_ATTACH_FAILURE`` for live attach faults.
        """
        ...
