"""Remote-libvirt Retrieve facade: kdump, host_dump, and crash postmortem (ADR-0084)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import libvirt

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.crash_postmortem import (
    FetchObject,
    ReadBuildId,
    RunCrash,
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
)
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.retrieve.common import (
    MAX_CORE_BYTES,
    AgentExecFactory,
    CoreBuildIdFromFile,
    CoreDmesgFromFile,
    Monotonic,
    OpenRetrieveConnection,
    Sleep,
    StorePort,
    open_libvirt_capture,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import (
    DMESG_UNAVAILABLE as _DMESG_UNAVAILABLE,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import (
    HostDumpCapturer,
    HostDumpOptions,
    host_dump_volume_name,
    read_core_build_id_from_file,
    read_core_dmesg_from_file,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import (
    file_sha256_b64 as _file_sha256_b64,
)
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import (
    pool_type_and_target as _pool_type_and_target,
)
from kdive.providers.remote_libvirt.retrieve.kdump_capture import (
    DEFAULT_PUT_EXPIRY_S,
    DEFAULT_READINESS_POLL_S,
    DEFAULT_READINESS_TIMEOUT_S,
    KdumpCapturer,
)
from kdive.providers.remote_libvirt.retrieve.postmortem import CrashPostmortemAdapter
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env


class RemoteLibvirtRetrieve:
    """The realized remote `Retriever` + `CrashPostmortem` facade (ADR-0084)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenRetrieveConnection = open_libvirt_capture,
        store_factory: Callable[[], StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        agent_exec_factory: AgentExecFactory | None = None,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        put_expiry_s: int = DEFAULT_PUT_EXPIRY_S,
        readiness_timeout_s: float = DEFAULT_READINESS_TIMEOUT_S,
        readiness_poll_s: float = DEFAULT_READINESS_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        fetch_object: FetchObject = default_fetch_object,
        read_build_id: ReadBuildId = default_read_vmcore_build_id,
        run_crash: RunCrash = default_run_crash,
        core_build_id_from_file: CoreBuildIdFromFile = read_core_build_id_from_file,
        core_dmesg_from_file: CoreDmesgFromFile = read_core_dmesg_from_file,
        host_dump_format: int = libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_RAW,
        max_core_bytes: int = MAX_CORE_BYTES,
    ) -> None:
        secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._kdump = KdumpCapturer(
            secret_registry=secret_registry,
            config_factory=config_factory,
            open_connection=open_connection,
            store_factory=store_factory,
            agent_command=agent_command,
            agent_exec_factory=agent_exec_factory,
            secret_backend_factory=secret_backend_factory,
            pki_base_dir=pki_base_dir,
            put_expiry_s=put_expiry_s,
            readiness_timeout_s=readiness_timeout_s,
            readiness_poll_s=readiness_poll_s,
            sleep=sleep,
            monotonic=monotonic,
        )
        self._host_dump = HostDumpCapturer(
            secret_registry=secret_registry,
            config_factory=config_factory,
            open_connection=open_connection,
            store_factory=store_factory,
            secret_backend_factory=secret_backend_factory,
            pki_base_dir=pki_base_dir,
            options=HostDumpOptions(
                core_build_id_from_file=core_build_id_from_file,
                core_dmesg_from_file=core_dmesg_from_file,
                dump_format=host_dump_format,
                max_core_bytes=max_core_bytes,
            ),
        )
        self._postmortem = CrashPostmortemAdapter(
            secret_registry=secret_registry,
            fetch_object=fetch_object,
            read_build_id=read_build_id,
            run_crash=run_crash,
        )

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtRetrieve:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a vmcore by dispatching to the selected remote-libvirt workflow."""
        if method is CaptureMethod.HOST_DUMP:
            return self._host_dump.capture(system_id)
        if method is CaptureMethod.KDUMP:
            return self._kdump.capture(system_id)
        raise CategorizedError(
            "remote-libvirt capture supports only the kdump and host_dump methods",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"method": method.value},
        )

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Delegate to the provider-neutral worker-side crash postmortem (ADR-0084)."""
        return self._postmortem.run(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
        )


__all__ = [
    "RemoteLibvirtRetrieve",
    "_DMESG_UNAVAILABLE",
    "_file_sha256_b64",
    "_pool_type_and_target",
    "host_dump_volume_name",
    "read_core_build_id_from_file",
    "read_core_dmesg_from_file",
]
