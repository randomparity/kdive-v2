"""Remote-libvirt introspection ports (ADR-0079/0083).

`RemoteVmcoreIntrospect` runs the offline drgn path on the worker (fetch core + vmlinux, verify
build-id provenance, run the shared helpers, redact + byte-cap) — no live reachability.
`RemoteLiveIntrospect` runs the in-guest drgn helper via the guest-agent seam. Both reuse
``debug_common.introspect.assemble_report`` as the single redaction boundary. The drgn open/exec
paths are ``live_vm``-gated; orchestration, provenance, and error contracts are unit-tested with
fakes.
"""

from __future__ import annotations

import json as _json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.drgn_program import (
    open_vmcore_program,
    read_vmcoreinfo_build_id,
    run_introspection_helper,
)
from kdive.providers.debug_common.introspect import assemble_report
from kdive.providers.ports import IntrospectOutput
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_REPORT_BYTE_CAP = 1 << 20

# The single allowlisted in-guest drgn helper the base image carries (ADR-0079); it runs the
# fixed in-tree helper named by argv[1] against /proc/kcore and prints that section as JSON.
_DRGN_HELPER = "/usr/local/sbin/kdive-drgn"
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})

type _OpenConnection = Callable[[str], Any]


class _Program(Protocol):
    def iter_tasks(self) -> list[object]: ...
    def iter_modules(self) -> list[object]: ...
    def uts(self) -> dict[str, str]: ...
    def boot_cmdline(self) -> str: ...
    def cpus_online(self) -> int: ...
    def mem_total_pages(self) -> int: ...


type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class RemoteVmcoreIntrospect:
    """Worker-side offline drgn introspection of a remote-captured vmcore (ADR-0033/0083)."""

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._secret_registry = secret_registry
        self._open_program = open_program
        self._run_helper = run_helper

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteVmcoreIntrospect:
        """Build from env with the real drgn seams (lazy: drgn imports on first use).

        drgn stays an operator-provided live-host prerequisite — the seams import it
        inside the call, so composition builds on hosts without it and ``from_vmcore``
        raises the documented ``MISSING_DEPENDENCY`` there instead of an import error.
        """
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=read_vmcoreinfo_build_id,
            secret_registry=secret_registry,
            open_program=open_vmcore_program,
            run_helper=run_introspection_helper,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` off the ``live_vm`` gate;
                ``CONFIGURATION_ERROR`` for a build-id provenance mismatch;
                ``INFRASTRUCTURE_FAILURE`` for object-store IO; ``DEBUG_ATTACH_FAILURE`` if drgn
                cannot open the core.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected_build_id:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=_REPORT_BYTE_CAP,
            secret_registry=self._secret_registry,
        )

    def _open(self, core: Path, vmlinux: Path) -> _Program:
        assert self._open_program is not None
        try:
            return self._open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env().get_artifact(ref, None).data


class RemoteLiveIntrospect:
    """In-guest drgn-live over the qemu-guest-agent seam (ADR-0079/0083 §4).

    ``transport_handle`` carries the guest **domain name** (the pinned ADR-0083 §4 contract).
    The helper is validated worker-side against the fixed set before any agent round-trip, and
    the real ``GuestAgentExec`` enforces the single-program allowlist, so a guest-agent exec can
    never run an arbitrary program. The single redaction boundary is ``assemble_report``. All
    slow/host seams (the agent round-trip, the libvirt opener, the secret backend) are injected,
    so unit tests drive the full two-phase protocol and the allowlist with no libvirt host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: _OpenConnection | None = None,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection if open_connection is not None else _open_libvirt
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLiveIntrospect:
        """Build from env; opens no connection (config read per op)."""
        return cls(secret_registry=secret_registry)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        """Run one allowlisted in-guest drgn helper; return a redacted, byte-bounded report.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown helper or a blank handle;
                ``TRANSPORT_FAILURE`` for an unreachable guest agent; ``INFRASTRUCTURE_FAILURE``
                for a malformed agent reply or undecodable helper output; ``DEBUG_ATTACH_FAILURE``
                for a non-zero helper exit (drgn could not attach in-guest).
        """
        if helper not in _LIVE_HELPERS:
            raise CategorizedError(
                f"unknown live introspection helper: {helper}",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        domain_name = transport_handle.strip()
        if not domain_name:
            raise CategorizedError(
                "remote live introspection handle must carry a domain name",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        section = self._run_in_guest(domain_name, helper)
        tasks = section if helper == "tasks" else {}
        modules = section if helper == "modules" else {}
        sysinfo = section if helper == "sysinfo" else {}
        return assemble_report(
            tasks,
            modules,
            sysinfo,
            byte_cap=_REPORT_BYTE_CAP,
            secret_registry=self._secret_registry,
        )

    def _run_in_guest(self, domain_name: str, helper: str) -> dict[str, object]:
        result = self._exec(domain_name, [_DRGN_HELPER, helper])
        if result.exit_status != 0:
            raise CategorizedError(
                "in-guest drgn helper exited non-zero (could not attach to the live kernel)",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"domain": domain_name, "exit_status": result.exit_status},
            )
        try:
            decoded = _json.loads(result.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CategorizedError(
                "in-guest drgn helper returned undecodable JSON",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if not isinstance(decoded, dict):
            raise CategorizedError(
                "in-guest drgn helper output was not a JSON object",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return decoded

    def _exec(self, domain_name: str, argv: list[str]) -> AgentExecResult:
        """Open the qemu+tls connection, look up the domain, run argv via GuestAgentExec.

        Fully unit-testable with an injected ``agent_command`` + ``open_connection`` +
        ``secret_backend_factory`` (mirroring install.py); only ``_open_libvirt``'s real
        ``libvirt.open`` is the ``live_vm`` seam.
        """
        agent = GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_DRGN_HELPER}),
        )
        config = self._config_factory()
        with remote_connection(
            config, self._secret_backend_factory(), open_connection=self._open_connection
        ) as conn:
            domain = conn.lookupByName(domain_name)
            return agent.run(domain, argv)


def _open_libvirt(uri: str) -> Any:  # pragma: no cover - live_vm
    return libvirt.open(uri)


__all__ = ["RemoteLiveIntrospect", "RemoteVmcoreIntrospect"]
