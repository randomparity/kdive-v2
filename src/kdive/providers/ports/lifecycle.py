"""Provision, install, connect, and control provider contracts."""

from __future__ import annotations

from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import PowerAction
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports._common import config_error
from kdive.providers.ports.handles import SystemHandle, TransportHandle

_TRANSPORT_KINDS = frozenset({"gdbstub", "ssh"})


class TransportHandleData(NamedTuple):
    """A decoded transport handle: the transport kind and its loopback endpoint."""

    kind: str
    host: str
    port: int

    def encode(self) -> str:
        """Serialize to the ``<kind>://host:port`` wire form."""
        return f"{self.kind}://{self.host}:{self.port}"

    @classmethod
    def decode(cls, raw: str) -> TransportHandleData:
        """Parse a serialized ``<kind>://host:port`` handle."""
        scheme, sep, remainder = raw.partition("://")
        if not sep or scheme not in _TRANSPORT_KINDS:
            raise config_error("transport handle has no known transport scheme")
        host, sep, port_text = remainder.rpartition(":")
        if not sep or not host:
            raise config_error("transport handle must be <kind>://host:port")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise config_error("transport handle port must be numeric") from exc
        if port <= 0 or port > 65535:
            raise config_error("transport handle port is outside 1..65535")
        return cls(scheme, host, port)


class Provisioner(Protocol):
    """Provisioning port keyed on the already-minted System id."""

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Create and start a System, returning the provider domain name.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``MISSING_DEPENDENCY`` for unavailable provider tools or materialization
                seams, ``PROVISIONING_FAILURE`` for domain/rootfs creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for provider-control-plane faults.
        """
        ...

    def teardown(self, domain_name: str) -> None:
        """Destroy provider state for a domain name.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the provider cannot complete
                or verify teardown.
        """
        ...

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Replace a System's provider state, returning the new provider domain name.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``PROVISIONING_FAILURE`` for replacement-domain creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for teardown/control-plane faults.
        """
        ...


class Installer(Protocol):
    """Install port keyed on System and Run ids."""

    def install(
        self,
        system_id: UUID,
        run_id: UUID,
        kernel_ref: str,
        *,
        cmdline: str,
        method: CaptureMethod = CaptureMethod.HOST_DUMP,
        initrd_ref: str | None = None,
    ) -> None:
        """Install a built kernel into a System and confirm guest readiness.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid capture/install inputs,
                ``STALE_HANDLE`` for vanished artifact refs, ``INFRASTRUCTURE_FAILURE`` for
                store IO failures, ``INSTALL_FAILURE`` for provider install faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Booter(Protocol):
    """Boot port: power-cycle the domain and confirm run-readiness."""

    def boot(self, system_id: UUID) -> None:
        """Boot a System after installation and confirm run-readiness.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for provider boot faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Connector(Protocol):
    """Connect port for opening and closing debug transports."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        """Open a debug transport and return an opaque handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown transport kind,
                ``MISSING_DEPENDENCY`` for unavailable provider seams,
                ``TRANSPORT_FAILURE`` for tunnel allocation faults, or
                ``DEBUG_ATTACH_FAILURE`` when the endpoint cannot be attached.
        """
        ...

    def close_transport(self, handle: TransportHandle) -> None:
        """Close a previously opened transport handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed handles,
                ``MISSING_DEPENDENCY`` for unavailable provider seams, or
                ``TRANSPORT_FAILURE`` when teardown of the tunnel fails.
        """
        ...


class Controller(Protocol):
    """Control port keyed on provider domain name."""

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Apply a power operation to a provider domain.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider power
                faults.
        """
        ...

    def force_crash(self, domain_name: str) -> None:
        """Trigger a guest crash path for vmcore capture.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider crash
                trigger faults.
        """
        ...
