"""Remote-libvirt Retrieve plane: two-phase vmcore capture + crash postmortem (ADR-0084).

`capture()` runs after a crash against a System whose guest must have rebooted out of the
kdump capture kernel: it waits out a still-rebooting agent (readiness), inspects the local
core (digest/size/build-id + a bounded inline redacted dmesg), mints a single presigned PUT
for a deterministic key, runs the in-guest upload through the registered-URL artifact channel
(ADR-0078 §2), and references the uploaded object via `head` (presence + etag; the signed
checksum is the integrity binding). The redacted dmesg is redacted again worker-side and
persisted inline. `run_crash_postmortem()` delegates to the shared `debug_common` helper.
KDUMP-only (host-dump is host-coupled). All host/S3/clock seams are injected.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
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
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
    artifact_key,
)
from kdive.providers.debug_common.crash_postmortem import (
    FetchObject,
    ReadBuildId,
    RunCrash,
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
)
from kdive.providers.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.providers.remote_libvirt.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_HELPER = "/usr/local/sbin/kdive-capture-vmcore"
_TENANT = "remote-libvirt"
_RETENTION = "vmcore"
_OWNER_KIND = "systems"
# One object + one checksum; lifetime must cover the in-guest upload of a multi-hundred-MB core.
_DEFAULT_PUT_EXPIRY_S = 3600
# S3 single-PUT ceiling (ADR-0048); larger cores are a multipart follow-up.
_MAX_CORE_BYTES = 5 * 1024**3
_DEFAULT_READINESS_TIMEOUT_S = 300.0
_DEFAULT_READINESS_POLL_S = 2.0
# The inspect command hashes the whole core in-guest; 120s comfortably covers a sha256 of
# the 5 GiB ceiling (~10s at commodity disk/CPU rates). GuestAgentExec folds a command that
# does not exit within this bound into TRANSPORT_FAILURE, which _await_inspect treats as
# "still rebooting" — acceptable because a working inspect finishes well inside the bound, so
# a TRANSPORT_FAILURE in practice means an unreachable agent, not a slow hash.
_DEFAULT_INSPECT_TIMEOUT_S = 120.0
_DEFAULT_UPLOAD_TIMEOUT_S = 1800.0
# An unreachable agent during readiness is "still rebooting out of the kdump kernel". A
# non-rebooting CategorizedError (a malformed reply -> INFRASTRUCTURE_FAILURE) is NOT in this
# set, so _await_inspect re-raises it immediately instead of spinning the readiness window.
_AGENT_REBOOTING = frozenset({ErrorCategory.TRANSPORT_FAILURE})


class _CoreInfo(NamedTuple):
    sha256: str
    size_bytes: int
    build_id: str
    dmesg: bytes


class _StorePort(Protocol):
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...
    def head(self, key: str) -> HeadResult | None: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _RetrieveConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


class _AgentExec(Protocol):
    def run(self, domain: Any, argv: list[str]) -> AgentExecResult: ...


type OpenRetrieveConnection = Callable[[str], _RetrieveConn]
type AgentExecFactory = Callable[[float], _AgentExec]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_capture(uri: str) -> _RetrieveConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


class RemoteLibvirtRetrieve:
    """The realized remote `Retriever` + `CrashPostmortem` (ADR-0084)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenRetrieveConnection = open_libvirt_capture,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        agent_exec_factory: AgentExecFactory | None = None,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        put_expiry_s: int = _DEFAULT_PUT_EXPIRY_S,
        readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S,
        readiness_poll_s: float = _DEFAULT_READINESS_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        fetch_object: FetchObject = default_fetch_object,
        read_build_id: ReadBuildId = default_read_vmcore_build_id,
        run_crash: RunCrash = default_run_crash,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._agent_exec_factory = agent_exec_factory or self._default_agent_exec
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._put_expiry_s = put_expiry_s
        self._readiness_timeout_s = readiness_timeout_s
        self._readiness_poll_s = readiness_poll_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._fetch_object = fetch_object
        self._read_build_id = read_build_id
        self._run_crash = run_crash

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtRetrieve:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Two-phase vmcore capture: inspect -> presign -> in-guest upload -> reference.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a non-KDUMP method or an
                over-ceiling core; ``READINESS_FAILURE`` when the guest never becomes
                reachable or carries no core; ``TRANSPORT_FAILURE`` for an agent fault
                outside the readiness window; ``INFRASTRUCTURE_FAILURE`` for an upload
                failure, a malformed reply, or an object absent after a success-reporting
                upload.
        """
        if method is not CaptureMethod.KDUMP:
            raise CategorizedError(
                "remote-libvirt capture supports only the kdump method",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"method": method.value},
            )
        config = self._config_factory()
        raw_key = artifact_key(_TENANT, _OWNER_KIND, str(system_id), f"vmcore-{method.value}")
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            info = self._await_inspect(domain, system_id)
            upload = self._store_factory().presign_put(
                PresignPutRequest(
                    key=raw_key,
                    sha256=info.sha256,
                    size_bytes=info.size_bytes,
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class=_RETENTION,
                    expires_in=self._put_expiry_s,
                )
            )
            self._upload(domain, system_id, upload)
        raw = self._reference(raw_key, info.sha256, system_id)
        redacted = self._persist_redacted(system_id, method, info.dmesg)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=info.build_id)

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Delegate to the provider-neutral worker-side crash postmortem (ADR-0084)."""
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )

    def _await_inspect(self, domain: _Domain, system_id: UUID) -> _CoreInfo:
        agent_exec = self._agent_exec_factory(_DEFAULT_INSPECT_TIMEOUT_S)
        deadline = self._monotonic() + self._readiness_timeout_s
        while True:
            try:
                result = agent_exec.run(domain, [_HELPER, "inspect"])
            except CategorizedError as exc:
                if exc.category not in _AGENT_REBOOTING:
                    raise
                if self._monotonic() >= deadline:
                    raise self._readiness_failure(
                        system_id, "guest agent never came back within the capture window"
                    ) from exc
                self._sleep(self._readiness_poll_s)
                continue
            return self._parse_inspect(result, system_id)

    def _parse_inspect(self, result: AgentExecResult, system_id: UUID) -> _CoreInfo:
        if result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore inspect exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8", "replace"))
            present = bool(payload["present"])
            sha256 = str(payload["sha256"])
            size_bytes = int(payload["size_bytes"])
            build_id = str(payload["build_id"])
            dmesg = base64.b64decode(payload["dmesg_b64"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CategorizedError(
                "guest vmcore inspect returned a malformed reply",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        if not present:
            raise self._readiness_failure(system_id, "no kdump core in the guest's dump storage")
        if size_bytes > _MAX_CORE_BYTES:
            raise CategorizedError(
                "captured core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "size_bytes": size_bytes},
            )
        return _CoreInfo(sha256=sha256, size_bytes=size_bytes, build_id=build_id, dmesg=dmesg)

    def _upload(self, domain: _Domain, system_id: UUID, upload: PresignedUpload) -> None:
        argv = [_HELPER, "upload", "--url", upload.url]
        for key, value in upload.required_headers.items():
            argv += ["--header", f"{key}:{value}"]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec_factory(_DEFAULT_UPLOAD_TIMEOUT_S),
            store_factory=self._store_factory,
            scope=object(),
        )
        output = channel.exec_with_capability(
            domain,
            capability_url=upload.url,
            argv=argv,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
        )
        if output.result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore upload exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": output.result.exit_status},
            )

    def _reference(self, raw_key: str, sha256: str, system_id: UUID) -> StoredArtifact:
        head = self._store_factory().head(raw_key)
        if head is None:
            raise CategorizedError(
                "uploaded vmcore is absent after a success-reporting upload",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256:
            raise CategorizedError(
                "uploaded vmcore checksum does not match the inspected core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        return StoredArtifact(raw_key, head.etag, Sensitivity.SENSITIVE, _RETENTION)

    def _persist_redacted(
        self, system_id: UUID, method: CaptureMethod, dmesg: bytes
    ) -> StoredArtifact:
        text = dmesg.decode("utf-8", "replace")
        redacted = Redactor(registry=self._secret_registry).redact_text(text)
        return self._store_factory().put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=f"vmcore-{method.value}-redacted",
                data=redacted.encode("utf-8"),
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )

    def _default_agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[_RetrieveConn]:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _RetrieveConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote domain lookup failed for capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc

    @staticmethod
    def _readiness_failure(system_id: UUID, reason: str) -> CategorizedError:
        return CategorizedError(
            reason,
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
        )


__all__ = ["RemoteLibvirtRetrieve"]
