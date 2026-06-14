"""Shared helpers for the runtime resource-mutation tools (M2.6 #396, ADR-0112).

``resources.register_*`` / ``deregister`` / ``renew`` are the imperative agent-native path
for runtime inventory mutation: they own ``managed_by='runtime'`` rows only, disjoint from
the declarative ``config`` rows the inventory reconciler owns. All are ``platform_admin``.

The reachability preflight is expressed through a small injectable :class:`ResourceProbe`
port (mirroring :class:`kdive.providers.build_host.reachability.BuildHostProber`) so the tool
stays a synchronous server-side primitive without importing the worker transport plane, and so
tests drive the probe deterministically. The default :class:`TcpResourceProbe` does a bounded
TCP connect to the host's ``host:port``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.mcp.responses import ToolResponse
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secrets import read_secret_file

_log = logging.getLogger(__name__)

REGISTER_REMOTE_LIBVIRT_TOOL = "resources.register_remote_libvirt"
REGISTER_LOCAL_LIBVIRT_TOOL = "resources.register_local_libvirt"
REGISTER_FAULT_INJECT_TOOL = "resources.register_fault_inject"
DEREGISTER_TOOL = "resources.deregister"
RENEW_TOOL = "resources.renew"

# The three runtime-registerable kinds, keyed by their systems.toml block name. local-libvirt
# is intentionally absent: those rows are discovery-owned (real hardware enumeration), never
# imperatively registered (ADR-0112).
_KIND_BY_BLOCK: dict[str, ResourceKind] = {
    "remote_libvirt": ResourceKind.REMOTE_LIBVIRT,
    "local_libvirt": ResourceKind.LOCAL_LIBVIRT,
    "fault_inject": ResourceKind.FAULT_INJECT,
}

# Default libvirt TLS port for a qemu+tls:// URI that omits an explicit port.
_DEFAULT_LIBVIRT_TLS_PORT = 16514
_DEFAULT_PROBE_TIMEOUT_S = 5.0


def resolve_block_kind(block: str) -> ResourceKind | None:
    """Map a ``systems.toml`` block name to its :class:`ResourceKind`, or ``None``."""
    return _KIND_BY_BLOCK.get(block)


def denied(object_id: str, tool: str) -> ToolResponse:
    """The authorization-denied envelope, pointing the caller back at the tool."""
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def config_error(object_id: str, reason: str) -> ToolResponse:
    """A configuration-error envelope carrying a human-readable ``reason``."""
    return ToolResponse.failure(
        object_id, ErrorCategory.CONFIGURATION_ERROR, data={"reason": reason}
    )


def secret_ref_resolves(ref: str, root: Path) -> bool:
    """Whether ``ref`` resolves to an existing secret file under ``root``.

    Validates existence only via :func:`read_secret_file` (confined to ``root``, size-capped);
    the value is read to confirm the reference but is **never** registered, logged, or returned,
    so this preflight carries no secret material.
    """
    try:
        read_secret_file(root, ref)
    except (PathSafetyError, OSError):
        return False
    return True


@runtime_checkable
class ResourceProbe(Protocol):
    """Report whether a resource host is reachable for the register preflight (ADR-0112)."""

    async def probe(self, host_uri: str) -> bool: ...


class TcpResourceProbe:
    """Default reachability probe: a bounded TCP connect to the host's ``host:port``.

    Lightweight and server-safe (no libvirt/secret plane). A URI with no resolvable host is
    treated as unreachable (fail-closed). The blocking connect is bounded by ``timeout_s``.
    """

    def __init__(self, *, timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    async def probe(self, host_uri: str) -> bool:
        """Return whether a TCP connection to ``host_uri``'s host:port succeeds; never raises."""
        target = _host_port(host_uri)
        if target is None:
            return False
        host, port = target
        try:
            fut = asyncio.open_connection(host, port)
            _, writer = await asyncio.wait_for(fut, timeout=self._timeout_s)
        except (OSError, TimeoutError):
            _log.info("resource host %r unreachable on tcp %s:%d", host_uri, host, port)
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True


def _host_port(host_uri: str) -> tuple[str, int] | None:
    """Extract ``(host, port)`` from a provider host URI, or ``None`` if no host is present."""
    parsed = urlsplit(host_uri)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port if parsed.port is not None else _DEFAULT_LIBVIRT_TLS_PORT
    return host, port


__all__ = [
    "DEREGISTER_TOOL",
    "REGISTER_FAULT_INJECT_TOOL",
    "REGISTER_LOCAL_LIBVIRT_TOOL",
    "REGISTER_REMOTE_LIBVIRT_TOOL",
    "RENEW_TOOL",
    "ResourceProbe",
    "TcpResourceProbe",
    "config_error",
    "denied",
    "resolve_block_kind",
    "secret_ref_resolves",
]
