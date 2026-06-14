"""Remote-libvirt in-guest kdump capture workflow."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import PresignPutRequest, StoredArtifact, artifact_key
from kdive.providers.ports import CaptureOutput
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.endpoint_preflight import validate_guest_routable_endpoint
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
)
from kdive.providers.remote_libvirt.guest.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.retrieve.common import (
    HELPER,
    MAX_CORE_BYTES,
    OWNER_KIND,
    RETENTION,
    TENANT,
    AgentExecFactory,
    CoreInfo,
    Domain,
    Monotonic,
    OpenRetrieveConnection,
    Sleep,
    StorePort,
    connection,
    lookup,
    persist_redacted,
    readiness_failure,
)
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend

DEFAULT_PUT_EXPIRY_S = 3600
DEFAULT_READINESS_TIMEOUT_S = 300.0
DEFAULT_READINESS_POLL_S = 2.0
DEFAULT_INSPECT_TIMEOUT_S = 120.0
DEFAULT_UPLOAD_TIMEOUT_S = 1800.0
AGENT_REBOOTING = frozenset({ErrorCategory.TRANSPORT_FAILURE})


class KdumpCapturer:
    """In-guest two-phase kdump capture over the guest-agent artifact channel."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig],
        open_connection: OpenRetrieveConnection,
        store_factory: Callable[[], StorePort],
        agent_command: AgentCommand,
        agent_exec_factory: AgentExecFactory | None,
        secret_backend_factory: Callable[[], SecretBackend],
        pki_base_dir: Path | None,
        put_expiry_s: int,
        readiness_timeout_s: float,
        readiness_poll_s: float,
        sleep: Sleep,
        monotonic: Monotonic,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._agent_exec_factory = agent_exec_factory or self._default_agent_exec
        self._secret_backend_factory = secret_backend_factory
        self._pki_base_dir = pki_base_dir
        self._put_expiry_s = put_expiry_s
        self._readiness_timeout_s = readiness_timeout_s
        self._readiness_poll_s = readiness_poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    def capture(self, system_id: UUID) -> CaptureOutput:
        """Inspect the guest's kdump core and upload it via a presigned PUT.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when ``KDIVE_S3_ENDPOINT_URL`` is a
                loopback/localhost address the remote guest cannot upload to (preflight,
                ADR-0110), before any guest round-trip; plus the inspect/upload failures the
                seams raise.
        """
        config = self._config_factory()
        validate_guest_routable_endpoint()
        method = CaptureMethod.KDUMP
        raw_key = artifact_key(TENANT, OWNER_KIND, str(system_id), f"vmcore-{method.value}")
        with connection(
            config, self._secret_backend_factory, self._open_connection, self._pki_base_dir
        ) as conn:
            domain = lookup(conn, domain_name_for(system_id))
            info = self._await_inspect(domain, system_id)
            upload = self._store_factory().presign_put(
                PresignPutRequest(
                    key=raw_key,
                    sha256=info.sha256,
                    size_bytes=info.size_bytes,
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class=RETENTION,
                    expires_in=self._put_expiry_s,
                )
            )
            self._upload(domain, system_id, upload)
        raw = self._reference(raw_key, info.sha256, system_id)
        redacted = persist_redacted(
            self._store_factory, self._secret_registry, system_id, method, info.dmesg
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=info.build_id)

    def _await_inspect(self, domain: Domain, system_id: UUID) -> CoreInfo:
        agent_exec = self._agent_exec_factory(DEFAULT_INSPECT_TIMEOUT_S)
        deadline = self._monotonic() + self._readiness_timeout_s
        while True:
            try:
                result = agent_exec.run(domain, [HELPER, "inspect"])
            except CategorizedError as exc:
                if exc.category not in AGENT_REBOOTING:
                    raise
                if self._monotonic() >= deadline:
                    raise readiness_failure(
                        system_id, "guest agent never came back within the capture window"
                    ) from exc
                self._sleep(self._readiness_poll_s)
                continue
            return self._parse_inspect(result, system_id)

    @staticmethod
    def _parse_inspect(result: AgentExecResult, system_id: UUID) -> CoreInfo:
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
            raise readiness_failure(system_id, "no kdump core in the guest's dump storage")
        if size_bytes > MAX_CORE_BYTES:
            raise CategorizedError(
                "captured core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "size_bytes": size_bytes},
            )
        return CoreInfo(sha256=sha256, size_bytes=size_bytes, build_id=build_id, dmesg=dmesg)

    def _upload(self, domain: Domain, system_id: UUID, upload: Any) -> None:
        argv = [HELPER, "upload", "--url", upload.url]
        for key, value in upload.required_headers.items():
            argv += ["--header", f"{key}:{value}"]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec_factory(DEFAULT_UPLOAD_TIMEOUT_S),
            store_factory=self._store_factory,
            scope=object(),
        )
        output = channel.exec_with_capability(
            domain,
            capability_url=upload.url,
            argv=argv,
            owner_kind=OWNER_KIND,
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
        return StoredArtifact(raw_key, head.etag, Sensitivity.SENSITIVE, RETENTION)

    def _default_agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
