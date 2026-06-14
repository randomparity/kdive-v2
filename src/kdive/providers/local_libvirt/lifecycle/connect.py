"""Local-libvirt Connect plane: a single-attach QEMU gdbstub transport (ADR-0032).

`LocalLibvirtConnect` realizes the handler-facing `Connector` port: `open_transport(system,
"gdbstub")` resolves the System's gdbstub endpoint, enforces loopback-only **before any
network IO** (the ported v1 "F2" SSRF control), probes RSP reachability over an injected
seam, and returns an opaque `TransportHandle` (an encoded `TransportHandleData`) the session
row persists; `close_transport(handle)` validates the handle and then no-ops (gdbstub is
connectionless RSP). The slow/host-bound steps — resolving the libvirt domain's gdbstub
host:port and the real socket probe — are **injected, `live_vm`-gated seams** that default
to implementations raising `MISSING_DEPENDENCY` (resolver) / `# pragma: no cover - live_vm`
(prober), so the orchestration and the full error contract are unit-tested with fakes.

The RSP-framing codec (`rsp_frame`/`valid_rsp_frame`) and the bounded probe are ported from
v1 `transport/core/rsp_probe.py` + `bounded.py`: the probe exchanges one **read-only** `?`
halt-reason query and accepts only a complete, checksum-valid `$...#xx` frame, so a stale or
non-RSP listener is rejected rather than mistaken for a healthy stub.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.rsp import rsp_reachable
from kdive.providers.ports import (
    DebugTransportKind,
    SystemHandle,
    TransportHandle,
    TransportHandleData,
)

_GDBSTUB: DebugTransportKind = "gdbstub"
_DRGN_LIVE: DebugTransportKind = "drgn-live"  # the agent-facing transport kind (ADR-0085)
_SSH_SCHEME = "ssh"  # the handle scheme local emits — its SSH realization (ADR-0039)

type _ResolveEndpoint = Callable[[SystemHandle], tuple[str, int]]
type _Probe = Callable[[str, int], bool]
type _SshConnect = Callable[[str, int], bool]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


def _is_loopback_literal(host: str) -> bool:
    """True iff ``host`` is a loopback IP literal (a hostname is not — reject without DNS)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalLibvirtConnect:
    """The realized `Connector` for local-libvirt transports: gdbstub and drgn-live.

    The agent-facing ``drgn-live`` transport (ADR-0085) is realized locally over SSH
    (ADR-0039); its handle keeps the ``ssh://`` scheme (a provider-internal realization
    detail). Both transports enforce loopback-only **before any network IO** (the ported v1
    "F2" SSRF control, ADR-0032 §5 / ADR-0039 §1) and probe reachability over an injected,
    ``live_vm``-gated seam: an RSP framing probe for gdbstub, an SSH connect for drgn-live.
    """

    def __init__(
        self,
        *,
        resolve_endpoint: _ResolveEndpoint,
        probe: _Probe,
        resolve_ssh_endpoint: _ResolveEndpoint | None = None,
        ssh_connect: _SshConnect | None = None,
    ) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._probe = probe
        self._resolve_ssh_endpoint = (
            resolve_ssh_endpoint if resolve_ssh_endpoint is not None else _real_resolve_ssh_endpoint
        )
        self._ssh_connect = ssh_connect if ssh_connect is not None else _real_ssh_connect

    @classmethod
    def from_env(cls) -> LocalLibvirtConnect:
        """Build with the real, ``live_vm``-gated resolvers + probers; opens no connection."""
        return cls(
            resolve_endpoint=_real_resolve_endpoint,
            probe=_real_probe,
            resolve_ssh_endpoint=_real_resolve_ssh_endpoint,
            ssh_connect=_real_ssh_connect,
        )

    def open_transport(self, system: SystemHandle, kind: DebugTransportKind) -> TransportHandle:
        """Open a single-attach transport (gdbstub or ssh) and return its handle.

        Resolves the System's endpoint, enforces loopback-only before any IO, and probes
        reachability. Runs no DB work — the caller owns the session row and the per-System
        lock (the probe deliberately runs lock-free, ADR-0032 §6a).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown kind or a non-loopback
                resolved host (no IO); ``DEBUG_ATTACH_FAILURE`` if the peer does not answer;
                ``TRANSPORT_FAILURE`` on a socket fault; ``MISSING_DEPENDENCY`` propagated
                from a real resolver outside ``live_vm``.
        """
        if kind == _GDBSTUB:
            return self._open_gdbstub(system)
        if kind == _DRGN_LIVE:
            return self._open_ssh(system)
        raise _config_error(f"unsupported transport kind: {kind!r}")

    def _open_gdbstub(self, system: SystemHandle) -> TransportHandle:
        host, port = self._resolve_endpoint(system)
        if not _is_loopback_literal(host):
            raise _config_error("gdbstub host must be a loopback IP literal")
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
                "gdbstub did not answer RSP framing",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"port": port},
            )
        return TransportHandle(TransportHandleData(kind=_GDBSTUB, host=host, port=port).encode())

    def _open_ssh(self, system: SystemHandle) -> TransportHandle:
        host, port = self._resolve_ssh_endpoint(system)
        if not _is_loopback_literal(host):
            raise _config_error("ssh host must be a loopback IP literal")
        try:
            reachable = self._ssh_connect(host, port)
        except OSError as exc:
            raise CategorizedError(
                "ssh transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "ssh endpoint did not accept a connection",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"port": port},
            )
        return TransportHandle(TransportHandleData(kind=_SSH_SCHEME, host=host, port=port).encode())

    def close_transport(self, handle: TransportHandle) -> None:
        """Validate the handle, then no-op for these connectionless transports."""
        TransportHandleData.decode(handle)


def _real_resolve_endpoint(system: SystemHandle) -> tuple[str, int]:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a libvirt domain's gdbstub endpoint runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system": str(system)},
    )


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


def _real_resolve_ssh_endpoint(
    system: SystemHandle,
) -> tuple[str, int]:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a libvirt guest's loopback-forwarded ssh endpoint runs only under "
        "the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system": str(system)},
    )


def _real_ssh_connect(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    """Open one SSH connection to prove reachability; True iff the handshake completes.

    Runs only under the ``live_vm`` gate — it needs a booted guest, a resolvable credential
    (already registered into the redaction registry by the caller), and the v1 SSH client.
    """
    raise CategorizedError(
        "the real ssh transport connect runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"port": port, "host": host},
    )


__all__ = ["LocalLibvirtConnect"]
