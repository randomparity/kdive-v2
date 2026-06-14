"""Remote-libvirt Connect plane: direct-TCP gdbstub transport (ADR-0079/0083).

`open_transport(system, "gdbstub")` composes the endpoint from operator config (the host is
``RemoteLibvirtConfig.gdb_addr``, the ACL'd listen address) and the per-System gdbstub port read
from the domain XML (ADR-0080), applies the ACL-remote host policy (no loopback gate — the host
is operator-trusted config, the operator ACL is the security boundary), probes RSP reachability,
and returns the encoded handle the gdb-MI tier consumes. The slow seams (domain-XML port read,
socket probe) are injected and ``live_vm``-gated; orchestration and the full error contract are
unit-tested with fakes. ``close_transport`` validates the handle and no-ops (connectionless RSP).
"""

from __future__ import annotations

from collections.abc import Callable

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.debug_common.rsp import rsp_reachable
from kdive.providers.ports import SystemHandle, TransportHandle, TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_inventory

_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"

type _ResolvePort = Callable[[SystemHandle], int]
type _Probe = Callable[[str, int], bool]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


class RemoteLibvirtConnect:
    """The realized remote ``Connector``: a single-attach direct-TCP gdbstub transport."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_inventory,
        resolve_port: _ResolvePort | None = None,
        probe: _Probe | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._resolve_port = resolve_port if resolve_port is not None else _real_resolve_port
        self._probe = probe if probe is not None else _real_probe

    @classmethod
    def from_env(cls) -> RemoteLibvirtConnect:
        """Build with the real ``live_vm``-gated domain-XML reader + socket probe."""
        return cls()

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        """Open the gdbstub or drgn-live transport for ``system``; raise for any other kind.

        ``drgn-live`` reaches in-guest drgn over the qemu-guest-agent keyed by domain, so its
        handle is the bare domain name core derived (``system``) — no gdb_addr, port, or probe
        (ADR-0083 §4). ``gdbstub`` composes the ACL'd direct-TCP endpoint and probes RSP.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown kind, an unset
                ``gdb_addr``, or a malformed host; ``DEBUG_ATTACH_FAILURE`` if the stub does
                not answer RSP; ``TRANSPORT_FAILURE`` on a socket fault; ``MISSING_DEPENDENCY``
                propagated from the real domain-XML reader outside ``live_vm``.
        """
        if kind == _DRGN_LIVE:
            return TransportHandle(str(system))
        if kind != _GDBSTUB:
            raise _config_error(f"unsupported transport kind: {kind!r}")
        config = self._config_factory()
        if not config.gdb_addr:
            raise _config_error("remote gdbstub host (instance gdb_addr in systems.toml) is unset")
        host = config.gdb_addr
        allow_acl_remote(host)
        port = self._resolve_port(system)
        try:
            reachable = self._probe(host, port)
        except OSError as exc:
            raise CategorizedError(
                "gdbstub transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "remote gdbstub did not answer RSP framing",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"host": host, "port": port},
            )
        return TransportHandle(TransportHandleData(kind=_GDBSTUB, host=host, port=port).encode())

    def close_transport(self, handle: TransportHandle) -> None:
        """No-op close. A schemed gdbstub handle is validated; the bare-domain drgn-live
        handle (ADR-0083 §4) is connectionless and needs no validation."""
        if "://" in str(handle):
            TransportHandleData.decode(handle)


def _real_resolve_port(system: SystemHandle) -> int:  # pragma: no cover - live_vm
    raise CategorizedError(
        "reading a remote domain's recorded gdbstub port runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system": str(system)},
    )


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


__all__ = ["RemoteLibvirtConnect"]
