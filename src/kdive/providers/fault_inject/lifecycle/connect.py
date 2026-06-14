"""Fault-inject Connect plane."""

from __future__ import annotations

import hashlib
from typing import cast

from kdive.providers.ports import (
    DEBUG_TRANSPORT_KINDS,
    DebugTransportKind,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports._common import config_error
from kdive.providers.ports.lifecycle import TransportHandleData, TransportHandleKind

_LOOPBACK_HOST = "127.0.0.1"


def synthetic_port(handle: str) -> int:
    """Derive a stable loopback port in 1024..65535 from a domain handle."""
    digest = hashlib.blake2b(handle.encode(), digest_size=2).digest()
    return 1024 + int.from_bytes(digest, "big") % (65535 - 1024 + 1)


class FaultInjectConnect:
    """Connector port: open and close a loopback debug transport."""

    def open_transport(self, system: SystemHandle, kind: DebugTransportKind) -> TransportHandle:
        if kind not in DEBUG_TRANSPORT_KINDS:
            raise config_error(f"unknown transport kind {kind!r}")
        handle_kind = cast(TransportHandleKind, kind)
        endpoint = TransportHandleData(handle_kind, _LOOPBACK_HOST, synthetic_port(str(system)))
        return TransportHandle(endpoint.encode())

    def close_transport(self, handle: TransportHandle) -> None:
        TransportHandleData.decode(handle)
