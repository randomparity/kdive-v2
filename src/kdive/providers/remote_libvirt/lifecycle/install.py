"""Remote-libvirt Install + Boot plane: in-guest kernel install + boot-id readiness (ADR-0082).

`RemoteLibvirtInstall` realizes the `Installer` + `Booter` ports against a provisioned remote
disk-image System (ADR-0080). `install()` mints a single presigned GET for the built
vmlinuz+modules bundle (ADR-0081), then runs a constrained in-guest helper through the issue-3
registered-URL artifact channel (ADR-0078) to pull, extract, and add-or-replace the single
deterministic ``kdive`` grub slot with the method-conditional crashkernel cmdline (already
composed upstream by ``cmdline_for``). `boot()` reads the guest boot_id, runs the helper's
atomic select-slot + detached reboot, and confirms a fresh boot by the boot_id changing — the
readiness signal a console-less remote target affords.

Independent of ``local_libvirt`` (ADR-0076). All slow/host seams — the qemu+tls connection
opener, the guest-agent round-trip, the object store, the clock, sleep — are injected, so unit
tests drive the full orchestration and every error path with no libvirt host; the real
curl/tar/grub/reboot mechanics run only under the ``live_vm`` gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.ports import InstallRequest
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.guest.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

# The single allowlisted in-guest helper the base image carries (ADR-0082 §1); the only program
# this plane lets through GuestAgentExec.
_HELPER = "/usr/local/sbin/kdive-install-kernel"
_TRANSCRIPT_OWNER_KIND = "systems"
# The presigned GET must outlive a worst-case in-guest download of a hundreds-of-MB bundle
# (ADR-0081), not the shortest possible window (ADR-0082 §2).
_DEFAULT_GET_EXPIRY_S = 3600
# The install command downloads + extracts the bundle in-guest; allow well beyond the guest-agent
# default so a large bundle does not time out mid-install.
_DEFAULT_INSTALL_TIMEOUT_S = 1800.0
_DEFAULT_BOOT_TIMEOUT_S = 300.0
_DEFAULT_BOOT_POLL_S = 2.0
# Reboot tears down the guest agent, so the boot command and the post-reboot boot-id polls expect
# the agent to be unreachable / its reply truncated; those categories are swallowed as "still
# rebooting" rather than treated as failures (ADR-0082 §3).
_REBOOT_EXPECTED = frozenset(
    {ErrorCategory.TRANSPORT_FAILURE, ErrorCategory.INFRASTRUCTURE_FAILURE}
)


class _StorePort(Protocol):
    # Both methods: install() mints the GET and the InTargetArtifactChannel persists the
    # redacted transcript via put_artifact, and the one injected factory serves both.
    def presign_get(self, key: str, *, expires_in: int) -> str: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _InstallConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenInstallConnection = Callable[[str], _InstallConn]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_install(uri: str) -> _InstallConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtInstall:
    """The realized remote `Installer` + `Booter` (ADR-0082).

    Buildable without operator config (ADR-0076): ``KDIVE_REMOTE_LIBVIRT_*`` is read per op via
    ``config_factory``, never at construction.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenInstallConnection = open_libvirt_install,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        get_expiry_s: int = _DEFAULT_GET_EXPIRY_S,
        install_timeout_s: float = _DEFAULT_INSTALL_TIMEOUT_S,
        boot_timeout_s: float = _DEFAULT_BOOT_TIMEOUT_S,
        boot_poll_s: float = _DEFAULT_BOOT_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._get_expiry_s = get_expiry_s
        self._install_timeout_s = install_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._boot_poll_s = boot_poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtInstall:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def install(self, request: InstallRequest) -> None:
        """Pull the built bundle in-guest, install it, and write the boot entry.

        Mints one presigned GET for ``request.kernel_ref``, runs the allowlisted helper's
        ``install`` subcommand through the registered-URL artifact channel (so the bearer URL is
        masked in any persisted transcript), and replaces the deterministic ``kdive`` grub slot.
        Does not reboot — ``boot`` owns the power transition.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for a non-zero helper exit (incl. an in-guest
                curl 403/404 from a vanished object — the worker only mints the URL, it never
                fetches), ``TRANSPORT_FAILURE`` for an unreachable guest agent,
                ``INFRASTRUCTURE_FAILURE`` from the object store or a malformed agent reply,
                ``CONFIGURATION_ERROR`` for missing operator config, propagated from the seams.
        """
        config = self._config_factory()
        url = self._store_factory().presign_get(request.kernel_ref, expires_in=self._get_expiry_s)
        argv = [
            _HELPER,
            "install",
            "--url",
            url,
            "--cmdline",
            request.cmdline,
            "--method",
            request.method.value,
        ]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec(self._install_timeout_s),
            store_factory=self._store_factory,
            scope=object(),
        )
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(request.system_id))
            output = channel.exec_with_capability(
                domain,
                capability_url=url,
                argv=argv,
                owner_kind=_TRANSCRIPT_OWNER_KIND,
                owner_id=str(request.system_id),
            )
        if output.result.exit_status != 0:
            raise CategorizedError(
                "in-guest kernel install exited non-zero",
                category=ErrorCategory.INSTALL_FAILURE,
                details={
                    "system_id": str(request.system_id),
                    "exit_status": output.result.exit_status,
                },
            )

    def boot(self, system_id: UUID) -> None:
        """Reboot into the installed kernel and confirm a fresh boot by boot_id change.

        Reads the guest's pre-reboot boot_id, runs the helper's atomic select-``kdive``-slot +
        detached reboot, then polls boot_id until it differs from the baseline — proving a real
        boot transition (a stale agent connection cannot fake a new boot_id, ADR-0082 §3).

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for a domain lookup fault or a non-zero
                boot-id baseline read; ``TRANSPORT_FAILURE`` when the guest agent is unreachable
                before the reboot; ``BOOT_TIMEOUT`` when no fresh boot_id appears within the boot
                window (a panic/hang manifests as the agent never reconnecting).
        """
        config = self._config_factory()
        agent_exec = self._agent_exec(self._boot_timeout_s)
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            baseline = self._read_boot_id(agent_exec, domain, system_id)
            self._trigger_reboot(agent_exec, domain)
            self._await_fresh_boot(agent_exec, domain, baseline, system_id)

    def _read_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain, system_id: UUID) -> str:
        result = agent_exec.run(domain, [_HELPER, "boot-id"])
        if result.exit_status != 0:
            raise CategorizedError(
                "could not read the guest boot-id baseline",
                category=ErrorCategory.INSTALL_FAILURE,
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _trigger_reboot(self, agent_exec: GuestAgentExec, domain: _Domain) -> None:
        """Run the helper's atomic select+detached-reboot; a lost agent is the expected signal."""
        try:
            agent_exec.run(domain, [_HELPER, "boot"])
        except CategorizedError as exc:
            if exc.category not in _REBOOT_EXPECTED:
                raise
            # Expected: the reboot tore down the guest agent. Log it so a later BOOT_TIMEOUT
            # can be told apart from a reboot command that genuinely failed (ADR-0082 §3).
            _log.debug("reboot command lost the guest agent as expected: %s", exc)

    def _await_fresh_boot(
        self, agent_exec: GuestAgentExec, domain: _Domain, baseline: str, system_id: UUID
    ) -> None:
        deadline = self._monotonic() + self._boot_timeout_s
        while True:
            current = self._poll_boot_id(agent_exec, domain)
            if current is not None and current != baseline:
                return
            if self._monotonic() >= deadline:
                raise CategorizedError(
                    "system did not reboot into a fresh kernel within the boot window",
                    category=ErrorCategory.BOOT_TIMEOUT,
                    details={"system_id": str(system_id), "timeout_s": self._boot_timeout_s},
                )
            self._sleep(self._boot_poll_s)

    def _poll_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain) -> str | None:
        """One post-reboot boot-id read; ``None`` means "agent down / not ready, keep polling"."""
        try:
            result = agent_exec.run(domain, [_HELPER, "boot-id"])
        except CategorizedError as exc:
            if exc.category in _REBOOT_EXPECTED:
                _log.debug("boot-id poll: agent not back yet (%s)", exc.category.value)
                return None
            raise
        if result.exit_status != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _connection(self, config: RemoteLibvirtConfig):  # noqa: ANN202 - contextmanager passthrough
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _InstallConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote domain lookup failed for install/boot",
                category=ErrorCategory.INSTALL_FAILURE,
                details={"domain": domain_name},
            ) from exc


__all__ = ["RemoteLibvirtInstall"]
