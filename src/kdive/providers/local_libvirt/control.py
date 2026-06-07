"""Local-libvirt Control plane: power and force_crash a tagged domain (ADR-0028).

`LocalLibvirtControl` looks a domain up by name over an injected connection factory and
drives libvirt — `power(domain_name, action)` (`on->create`, `off->destroy`, `reset->reset`,
`cycle->reboot`) and `force_crash(domain_name)` (`injectNMI`). DB-free: it owns no Postgres;
the `control.*` handlers drive the state machine. The realized port keys on the libvirt
domain name (row-first ordering, ADR-0028 §1), distinct from the capability-dispatch
`ControlPlane` placeholder in `kdive.providers.interfaces`. Unit tests inject a fake
connection; the real `libvirt.open` adapter is `live_vm`-only.

`power on`/`power off` swallow the "already in the target state" libvirt error
(`VIR_ERR_OPERATION_INVALID`) as the achieved post-state (idempotent); an absent domain or
any other libvirt error is `CONTROL_FAILURE` — distinct from teardown's idempotent
absent-is-success, because you cannot power or crash a System whose domain is gone.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import Controller as Controller
from kdive.providers.ports import PowerAction

_log = logging.getLogger(__name__)

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def reset(self, flags: int) -> int: ...
    def reboot(self, flags: int) -> int: ...
    def injectNMI(self, flags: int) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtControl:
    """The `Controller` for the local libvirt host (power + force_crash)."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtControl:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        # The bound `virConnect` structurally satisfies the narrow `_LibvirtConn` Protocol
        # (only `lookupByName`/`close`), so no suppression is needed at this seam (ADR-0025).
        return cls(connect=lambda: libvirt.open(host_uri))

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Drive the domain's power state; idempotent ``on``/``off`` swallow the post-state.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or a
                non-idempotent libvirt error occurs.
        """
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            self._apply_power(domain, domain_name, action)
        finally:
            _close(conn)

    def force_crash(self, domain_name: str) -> None:
        """Panic the guest via NMI (``injectNMI``).

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or libvirt errors.
        """
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            try:
                domain.injectNMI(0)
            except libvirt.libvirtError as exc:
                raise self._control_failure("injecting NMI into", domain_name) from exc
        finally:
            _close(conn)

    def _open(self) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._control_failure("connecting to libvirt for", "control") from exc

    def _lookup(self, conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._control_failure("looking up", domain_name) from exc

    def _apply_power(self, domain: _LibvirtDomain, domain_name: str, action: PowerAction) -> None:
        try:
            if action is PowerAction.ON:
                self._idempotent(domain.create, "starting", domain_name)
            elif action is PowerAction.OFF:
                self._idempotent(domain.destroy, "stopping", domain_name)
            elif action is PowerAction.RESET:
                domain.reset(0)
            else:  # PowerAction.CYCLE
                domain.reboot(0)
        except libvirt.libvirtError as exc:
            raise self._control_failure(f"{action.value}-ing", domain_name) from exc

    @staticmethod
    def _idempotent(call: Callable[[], int], verb: str, domain_name: str) -> None:
        """Run an on/off call, swallowing the "already in target state" error as success."""
        try:
            call()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise
            _log.info("%s domain %s: already in target state; treating as ok", verb, domain_name)

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )
