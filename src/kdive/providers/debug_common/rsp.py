"""RSP framing codec + bounded reachability probe (ported v1, ADR-0032/0083).

Shared by every provider's gdbstub Connect plane: ``rsp_frame`` builds a ``$payload#xx``
packet, ``valid_rsp_frame`` validates a complete checksum-correct reply, and ``rsp_reachable``
exchanges one read-only ``?`` halt-reason query and accepts only a valid frame (a stale or
non-RSP listener is rejected). The real socket path runs only under the ``live_vm`` gate.
"""

from __future__ import annotations

import socket
import time

# Cap on bytes buffered while waiting for a complete RSP frame from an unauthenticated peer.
# A valid `$...#xx` halt-reason reply is a few dozen bytes; the bound stops a hostile peer
# that streams without ever sending `#` from pinning memory in the accumulation loop.
_RSP_MAX_ACCUMULATE_BYTES = 4096
_PROBE_TIMEOUT_S = 2.0


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
    start = 1 if buffer.startswith((b"+", b"-")) else 0
    if not buffer[start:].startswith(b"$"):
        return False
    hash_idx = buffer.find(b"#", start)
    if hash_idx == -1 or hash_idx + 3 != len(buffer):
        return False
    payload = buffer[start + 1 : hash_idx]
    checksum_hex = buffer[hash_idx + 1 : hash_idx + 3]
    try:
        expected = int(checksum_hex, 16)
    except ValueError:
        return False
    return (sum(payload) % 256) == expected


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


__all__ = ["rsp_frame", "rsp_reachable", "valid_rsp_frame"]
