"""Shared test doubles for the remote-libvirt provider suite."""

from __future__ import annotations

import libvirt


def libvirt_error(code: int) -> libvirt.libvirtError:
    """Build a libvirtError whose get_error_code() returns ``code``.

    Duplicated from the local-libvirt fakes deliberately — no shared layer
    (ADR-0076).
    """
    err = libvirt.libvirtError("synthetic")
    # get_error_code() reads self.err[0]; libvirtError leaves err=None with no live error.
    err.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


class RecordingBackend:
    """SecretBackend test double returning a distinct PEM body per ref."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> str:
        self.resolved.append(ref)
        return f"PEM::{ref}"


class FakeConn:
    """The slice of a libvirt connection the remote provider uses."""

    def __init__(self) -> None:
        self.closed = False

    def getInfo(self) -> list[object]:  # noqa: N802 - libvirt binding name
        return ["x86_64", 16384, 8, 2400, 1, 1, 8, 1]

    def getCapabilities(self) -> str:  # noqa: N802 - libvirt binding name
        return "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"

    def close(self) -> None:
        self.closed = True
