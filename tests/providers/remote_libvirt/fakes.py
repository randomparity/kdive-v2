"""Remote-libvirt control/capture test doubles (duplicated, no shared layer — ADR-0076)."""

from __future__ import annotations

import libvirt

from tests.providers.remote_libvirt.conftest import libvirt_error


class FakeDomain:
    """The domain slice the remote Control plane drives, recording calls."""

    def __init__(self, name: str, *, raise_on: dict[str, int] | None = None) -> None:
        self._name = name
        self._raise_on = raise_on or {}
        self.calls: list[str] = []

    def name(self) -> str:  # noqa: N802 - libvirt binding name
        return self._name

    def _maybe_raise(self, call: str) -> None:
        if call in self._raise_on:
            raise libvirt_error(self._raise_on[call])

    def create(self) -> int:
        self.calls.append("create")
        self._maybe_raise("create")
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self._maybe_raise("destroy")
        return 0

    def reset(self, flags: int) -> int:
        self.calls.append("reset")
        self._maybe_raise("reset")
        return 0

    def reboot(self, flags: int) -> int:
        self.calls.append("reboot")
        self._maybe_raise("reboot")
        return 0

    def injectNMI(self, flags: int) -> int:  # noqa: N802 - libvirt binding name
        self.calls.append("injectNMI")
        self._maybe_raise("injectNMI")
        return 0

    def qemuMonitorCommand(self, cmd: str, flags: int) -> str:  # noqa: N802 - libvirt binding name
        self.calls.append(f"monitor:{cmd}")
        self._maybe_raise("qemuMonitorCommand")
        return ""


class FakeControlConn:
    """A libvirt connection slice with lookupByName + close for the control fakes."""

    def __init__(self, lookup: dict[str, FakeDomain]) -> None:
        self._lookup = lookup
        self.closed = False

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802 - libvirt binding name
        try:
            return self._lookup[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN) from exc

    def close(self) -> None:
        self.closed = True
