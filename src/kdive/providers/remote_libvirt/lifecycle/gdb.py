"""Remote-libvirt gdbstub port allocation helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.xml import recorded_gdb_port

DOMAIN_PREFIX = "kdive-"


class Domain(Protocol):
    """The domain slice gdbstub enumeration uses."""

    def name(self) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802


class GdbPortConn(Protocol):
    """The connection slice gdbstub enumeration uses."""

    def listAllDomains(self, flags: int = 0) -> Sequence[Domain]: ...  # noqa: N802


def allocate_gdb_port(
    used: dict[str, int],
    *,
    own_name: str,
    port_min: int,
    port_max: int,
    exclude: set[int] | None = None,
) -> int:
    """Pick the System's gdbstub port from the configured range (ADR-0080 §2).

    Reuses the System's own recorded in-range port (stable across retries); otherwise
    the lowest port not recorded by another defined kdive domain and not in
    ``exclude`` (ports already tried in this attempt's bounded start-failure advance).

    Raises:
        CategorizedError: ``PROVISIONING_FAILURE`` when the range is exhausted.
    """
    own = used.get(own_name)
    if own is not None and port_min <= own <= port_max and (exclude is None or own not in exclude):
        return own
    taken = {port for name, port in used.items() if name != own_name}
    if exclude:
        taken |= exclude
    for port in range(port_min, port_max + 1):
        if port not in taken:
            return port
    raise CategorizedError(
        "gdbstub port range is exhausted on the remote host",
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"port_min": port_min, "port_max": port_max, "in_use": len(taken)},
    )


def used_gdb_ports(conn: GdbPortConn) -> dict[str, int]:
    """Ports recorded by defined kdive domains; a domain vanishing mid-walk is skipped."""
    used: dict[str, int] = {}
    try:
        domains = conn.listAllDomains()
    except libvirt.libvirtError as exc:
        raise _infra("listing domains for gdbstub port enumeration") from exc
    for domain in domains:
        try:
            name = domain.name()
            if not name.startswith(DOMAIN_PREFIX):
                continue
            port = recorded_gdb_port(domain.XMLDesc())
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                continue
            raise _infra("enumerating gdbstub ports") from exc
        if port is not None:
            used[name] = port
    return used


def _infra(verb: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={},
    )
