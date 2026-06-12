"""Fault-inject Retrieve and crash-postmortem planes."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.fault_inject._common import SYNTHETIC_BUILD_ID, TENANT, StorePort
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.security.artifacts.crash_commands import validate_crash_commands

_RETENTION_CLASS = "vmcore"


class FaultInjectRetrieve:
    """Retriever + CrashPostmortem ports: store a synthetic vmcore, symbolize synthetically."""

    def __init__(self, *, store_factory: Callable[[], StorePort]) -> None:
        self._store_factory = store_factory
        self._store: StorePort | None = None

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        raw = self._put(system_id, f"vmcore-{method.value}", Sensitivity.SENSITIVE)
        redacted = self._put(system_id, f"vmcore-{method.value}-redacted", Sensitivity.REDACTED)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=SYNTHETIC_BUILD_ID)

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        rejected = validate_crash_commands(commands)
        if rejected is not None:
            raise CategorizedError(
                "crash command batch rejected",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": rejected},
            )
        results: dict[str, object] = {command: "synthetic" for command in commands}
        return CrashOutput(results=results, transcript="fault-inject postmortem", truncated=False)

    def _put(self, system_id: UUID, name: str, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=TENANT,
                owner_kind="systems",
                owner_id=str(system_id),
                name=name,
                data=b"fault-inject-vmcore",
                sensitivity=sens,
                retention_class=_RETENTION_CLASS,
            )
        )
