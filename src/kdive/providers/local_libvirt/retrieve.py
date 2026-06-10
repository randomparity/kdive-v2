"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id, method)` dispatches to the appropriate seam, stores the raw
`sensitive` core and a `redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run_crash_postmortem(...)` symbolizes the core against the Run's
`debuginfo_ref` over an injected `crash` subprocess. The slow, host-bound operations are
`live_vm`-gated seams, so the orchestration and the full error contract are unit-tested with
fakes. The crash-command
validator is the load-bearing security control at the port boundary: every caller command is
sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.debug_common.crash_postmortem import (
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
)
from kdive.providers.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.ports import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_RETENTION_CLASS = "vmcore"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


type _WaitForVmcore = Callable[[UUID], bytes | None]
type _HostDumpCapture = Callable[[UUID], bytes | None]
type _ReadBuildId = Callable[[bytes], str]
type _ExtractRedacted = Callable[[bytes], bytes]
type _FetchObject = Callable[[str], bytes]
type _RunCrash = Callable[[Path, Path, str], CrashResult]


class LocalLibvirtRetrieve:
    """The realized Retrieve port: kdump capture + crash postmortem (ADR-0031)."""

    def __init__(
        self,
        *,
        tenant: str,
        store_factory: Callable[[], _StorePort],
        wait_for_vmcore: _WaitForVmcore,
        read_vmcore_build_id: _ReadBuildId,
        extract_redacted: _ExtractRedacted,
        host_dump_capture: _HostDumpCapture,
        secret_registry: SecretRegistry,
        fetch_object: _FetchObject | None = None,
        run_crash: _RunCrash | None = None,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._extract_redacted = extract_redacted
        self._host_dump_capture = host_dump_capture
        self._fetch_object = fetch_object
        self._run_crash = run_crash
        self._secret_registry = secret_registry

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=object_store_from_env,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=default_read_vmcore_build_id,
            extract_redacted=_real_extract_redacted,
            host_dump_capture=_real_host_dump_capture,
            fetch_object=default_fetch_object,
            run_crash=default_run_crash,
            secret_registry=secret_registry,
        )

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a core via ``method``; store raw + redacted; return refs + build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for capture/build-id provenance or
                input failures propagated by injected seams; ``MISSING_DEPENDENCY`` when a
                capture, build-id, or redaction seam is unavailable; ``READINESS_FAILURE``
                if no complete core appears in the window; or ``INFRASTRUCTURE_FAILURE``
                propagated from a failed artifact store.
        """
        if method is CaptureMethod.HOST_DUMP:
            data = self._host_dump_capture(system_id)
        else:  # CaptureMethod.KDUMP
            data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete core appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, f"vmcore-{method.value}", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id,
            f"vmcore-{method.value}-redacted",
            self._extract_redacted(data),
            Sensitivity.REDACTED,
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)

    def _put(self, system_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=self._tenant,
                owner_kind="systems",
                owner_id=str(system_id),
                name=name,
                data=data,
                sensitivity=sens,
                retention_class=_RETENTION_CLASS,
            )
        )

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

        Delegates to the provider-neutral worker-side helper (ADR-0084); raises the same
        categories.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a rejected crash command,
                malformed ref rejected by an injected fetch/build-id seam, or a build-id
                provenance mismatch;
                ``MISSING_DEPENDENCY`` if the crash seams were not configured;
                ``STALE_HANDLE`` when a referenced object is missing; or
                ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
        """
        if self._fetch_object is None or self._run_crash is None:
            raise CategorizedError(
                "crash seams not configured on this Retriever",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_vmcore_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )


_CRASH_DIR_ENV = "KDIVE_CRASH_DIR"


def _real_wait_for_vmcore(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real kdump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id), "crash_dir_env": _CRASH_DIR_ENV},
    )


def _real_host_dump_capture(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real host-dump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id)},
    )


def _real_extract_redacted(data: bytes) -> bytes:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore dmesg extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "LocalLibvirtRetrieve",
]
