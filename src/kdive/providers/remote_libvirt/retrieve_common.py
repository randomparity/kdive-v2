"""Shared primitives for remote-libvirt vmcore retrieval."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from uuid import UUID

import libvirt

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend

HELPER = "/usr/local/sbin/kdive-capture-vmcore"
TENANT = "remote-libvirt"
RETENTION = "vmcore"
OWNER_KIND = "systems"
MAX_CORE_BYTES = 5 * 1024**3


class CoreInfo(NamedTuple):
    """Inspected vmcore metadata."""

    sha256: str
    size_bytes: int
    build_id: str
    dmesg: bytes


class StorePort(Protocol):
    """Object-store operations used by remote retrieve capture paths."""

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...
    def head(self, key: str) -> HeadResult | None: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact: ...


class Domain(Protocol):
    def name(self) -> str: ...


class RetrieveConn(Protocol):
    def lookupByName(self, name: str) -> Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


class AgentExec(Protocol):
    def run(self, domain: Any, argv: list[str]) -> AgentExecResult: ...


type OpenRetrieveConnection = Callable[[str], RetrieveConn]
type AgentExecFactory = Callable[[float], AgentExec]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]
type CoreBuildIdFromFile = Callable[[Path], str]
type CoreDmesgFromFile = Callable[[Path], bytes]


def open_libvirt_capture(uri: str) -> RetrieveConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


def connection(
    config: RemoteLibvirtConfig,
    secret_backend_factory: Callable[[], SecretBackend],
    open_connection: OpenRetrieveConnection,
    pki_base_dir: Path | None,
) -> AbstractContextManager[RetrieveConn]:
    return remote_connection(
        config,
        secret_backend_factory(),
        open_connection=open_connection,
        pki_base_dir=pki_base_dir,
    )


def lookup(conn: RetrieveConn, domain_name: str) -> Domain:
    try:
        return conn.lookupByName(domain_name)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "remote domain lookup failed for capture",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        ) from exc


def readiness_failure(system_id: UUID, reason: str) -> CategorizedError:
    return CategorizedError(
        reason,
        category=ErrorCategory.READINESS_FAILURE,
        details={"system_id": str(system_id)},
    )


def persist_redacted(
    store_factory: Callable[[], StorePort],
    secret_registry: SecretRegistry,
    system_id: UUID,
    method: CaptureMethod,
    dmesg: bytes,
) -> StoredArtifact:
    text = dmesg.decode("utf-8", "replace")
    redacted = Redactor(registry=secret_registry).redact_text(text)
    return store_factory().put_artifact(
        ArtifactWriteRequest(
            tenant=TENANT,
            owner_kind=OWNER_KIND,
            owner_id=str(system_id),
            name=f"vmcore-{method.value}-redacted",
            data=redacted.encode("utf-8"),
            sensitivity=Sensitivity.REDACTED,
            retention_class=RETENTION,
        )
    )
