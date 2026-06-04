"""Local-libvirt Connect plane: a single-attach QEMU gdbstub transport (ADR-0032).

`LocalLibvirtConnect` realizes the handler-facing `Connector` port: `open_transport(system,
"gdbstub")` resolves the System's gdbstub endpoint, enforces loopback-only **before any
network IO** (the ported v1 "F2" SSRF control), probes RSP reachability over an injected
seam, and returns an opaque `TransportHandle` (an encoded `TransportHandleData`) the session
row persists; `close_transport(handle)` is a best-effort no-op (the M0 gdbstub is
connectionless RSP). The slow/host-bound steps — resolving the libvirt domain's gdbstub
host:port and the real socket probe — are **injected, `live_vm`-gated seams** that default to
implementations raising `MISSING_DEPENDENCY` (resolver) / `# pragma: no cover - live_vm`
(prober), so the orchestration and the full error contract are unit-tested with fakes.

The RSP-framing codec (`rsp_frame`/`valid_rsp_frame`) and the bounded probe are ported from
v1 `transport/core/rsp_probe.py` + `bounded.py`: the probe exchanges one **read-only** `?`
halt-reason query and accepts only a complete, checksum-valid `$...#xx` frame, so a stale or
non-RSP listener is rejected rather than mistaken for a healthy stub.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from typing import NamedTuple, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.interfaces import SystemHandle, TransportHandle

_GDBSTUB = "gdbstub"
_HANDLE_SCHEME = "gdbstub://"

# Cap on bytes buffered while waiting for a complete RSP frame from an unauthenticated peer.
# A valid `$...#xx` halt-reason reply is a few dozen bytes; the bound stops a hostile peer
# that streams without ever sending `#` from pinning memory in the accumulation loop.
_RSP_MAX_ACCUMULATE_BYTES = 4096
_PROBE_TIMEOUT_S = 2.0

type _ResolveEndpoint = Callable[[SystemHandle], tuple[str, int]]
type _Probe = Callable[[str, int], bool]


def rsp_frame(payload: str) -> bytes:
    """Wrap an RSP payload as ``$<payload>#<checksum>`` (mod-256 sum, 2 hex digits)."""
    checksum = sum(payload.encode("ascii")) % 256
    return b"$" + payload.encode("ascii") + b"#" + f"{checksum:02x}".encode("ascii")


def valid_rsp_frame(buffer: bytes) -> bool:
    """Report whether ``buffer`` holds a complete, checksum-valid RSP packet.

    A complete ``$<payload>#<2 hex>`` whose checksum equals ``sum(payload) % 256`` is valid
    (a leading ``+``/``-`` ack is ignored). A bare ``+``, an unterminated ``$...`` with no
    ``#``, a non-hex checksum, or a checksum mismatch is invalid — so a non-RSP listener that
    merely writes ``+`` or ``$hello`` is rejected.
    """
    start = buffer.find(b"$")
    if start == -1:
        return False
    hash_idx = buffer.find(b"#", start)
    if hash_idx == -1 or hash_idx + 2 >= len(buffer):
        return False
    payload = buffer[start + 1 : hash_idx]
    checksum_hex = buffer[hash_idx + 1 : hash_idx + 3]
    try:
        expected = int(checksum_hex, 16)
    except ValueError:
        return False
    return (sum(payload) % 256) == expected


class TransportHandleData(NamedTuple):
    """A decoded transport handle: the transport kind and its loopback endpoint.

    Encoded as ``gdbstub://<host>:<port>`` for the ``transport_handle`` column. It carries
    only provider-resolved, non-sensitive values (a loopback endpoint), never guest output.
    """

    kind: str
    host: str
    port: int

    def encode(self) -> str:
        """Serialize to the ``gdbstub://host:port`` wire form."""
        return f"{self.kind}://{self.host}:{self.port}"

    @classmethod
    def decode(cls, raw: str) -> TransportHandleData:
        """Parse a serialized handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if ``raw`` is not a well-formed
                ``gdbstub://host:port`` handle (the message names the shape, not the value).
        """
        if not raw.startswith(_HANDLE_SCHEME):
            raise _config_error("transport handle is not a gdbstub handle")
        remainder = raw[len(_HANDLE_SCHEME) :]
        host, sep, port_text = remainder.rpartition(":")
        if not sep or not host:
            raise _config_error("transport handle is missing host:port")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise _config_error("transport handle port is not an integer") from exc
        return cls(kind=_GDBSTUB, host=host, port=port)


class Connector(Protocol):
    """The handler-facing Connect port (the realized M0 contract), keyed on the System."""

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


def _is_loopback_literal(host: str) -> bool:
    """True iff ``host`` is a loopback IP literal (a hostname is not — reject without DNS)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalLibvirtConnect:
    """The realized `Connector` for the local libvirt gdbstub (open/close transport)."""

    def __init__(self, *, resolve_endpoint: _ResolveEndpoint, probe: _Probe) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._probe = probe

    @classmethod
    def from_env(cls) -> LocalLibvirtConnect:
        """Build with the real, ``live_vm``-gated resolver + prober; opens no connection."""
        return cls(resolve_endpoint=_real_resolve_endpoint, probe=_real_probe)

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        """Open a single-attach gdbstub transport and return its handle.

        Resolves the System's gdbstub endpoint, enforces loopback-only before any IO, and
        probes RSP reachability. Runs no DB work — the caller owns the session row and the
        per-System lock (the probe deliberately runs lock-free, ADR-0032 §6a).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a non-``gdbstub`` kind or a
                non-loopback resolved host (no IO); ``DEBUG_ATTACH_FAILURE`` if the stub does
                not answer RSP framing; ``TRANSPORT_FAILURE`` on a socket fault;
                ``MISSING_DEPENDENCY`` propagated from the real resolver outside ``live_vm``.
        """
        if kind != _GDBSTUB:
            raise _config_error(f"unsupported transport kind (M0 ships gdbstub only): {kind!r}")
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

    def close_transport(self, handle: TransportHandle) -> None:
        """Best-effort teardown — a no-op for the connectionless M0 gdbstub (never raises)."""
        del handle


def rsp_reachable(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    """Connect and exchange one read-only ``?`` RSP query; True iff a valid frame answers.

    A plain TCP listener that accepts but never answers (or answers garbage) returns False.
    The real socket path runs only under the ``live_vm`` gate.
    """
    deadline = time.monotonic() + _PROBE_TIMEOUT_S
    sock = socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S)
    buffer = b""
    try:
        sock.sendall(b"+" + rsp_frame("?"))
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue
            if not chunk:
                break
            buffer += chunk
            if valid_rsp_frame(buffer):
                return True
            if len(buffer) > _RSP_MAX_ACCUMULATE_BYTES:
                break
    finally:
        sock.close()
    return False


def _real_resolve_endpoint(system: SystemHandle) -> tuple[str, int]:  # pragma: no cover - live_vm
    raise CategorizedError(
        "resolving a libvirt domain's gdbstub endpoint runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system": str(system)},
    )


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


__all__ = [
    "Connector",
    "LocalLibvirtConnect",
    "TransportHandle",
    "TransportHandleData",
    "rsp_frame",
    "rsp_reachable",
    "valid_rsp_frame",
]
