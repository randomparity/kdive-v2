"""Fault-inject Build plane."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.fault_inject._common import SYNTHETIC_BUILD_ID, TENANT, StorePort


class FaultInjectBuild:
    """Builder port: store synthetic kernel + debuginfo and return their refs."""

    def __init__(self, *, store_factory: Callable[[], StorePort]) -> None:
        self._store_factory = store_factory
        self._store: StorePort | None = None

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        del profile
        kernel = self._put(run_id, "kernel", b"fault-inject-kernel", Sensitivity.REDACTED)
        debuginfo = self._put(run_id, "vmlinux", b"fault-inject-vmlinux", Sensitivity.REDACTED)
        return BuildOutput(
            kernel_ref=kernel.key, debuginfo_ref=debuginfo.key, build_id=SYNTHETIC_BUILD_ID
        )

    def _put(self, run_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=TENANT,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                data=data,
                sensitivity=sens,
                retention_class="kernel-build",
            )
        )
