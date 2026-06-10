"""Host-reachability policy for the shared gdb-MI/RSP transport (ADR-0083 §2).

A `HostPolicy` validates a resolved RSP host before any network IO, or raises
`CONFIGURATION_ERROR`. `require_loopback` is the local SSRF control (the endpoint is
resolved from a libvirt domain, so a non-loopback host is rejected without DNS).
`allow_acl_remote` is the remote policy: the host is `RemoteLibvirtConfig.gdb_addr`,
operator-trusted config, so it need not be loopback — only non-empty and free of control
whitespace. The operator ACL restricting the unauthenticated gdbstub to the worker pool is
the security boundary (ADR-0079), not a host-shape assertion.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

from kdive.domain.errors import CategorizedError, ErrorCategory

type HostPolicy = Callable[[str], None]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


def require_loopback(host: str) -> None:
    """Raise unless ``host`` is a loopback IP literal (a hostname is rejected — no DNS)."""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise _config_error(f"RSP host must be a loopback IP literal, got {host!r}")


def allow_acl_remote(host: str) -> None:
    """Raise unless ``host`` is a non-empty, control-whitespace-free operator-config address."""
    if not host or host != host.strip() or any(c in host for c in " \t\r\n"):
        raise _config_error(f"remote gdbstub host must be a non-blank address, got {host!r}")
