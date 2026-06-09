"""Shared test doubles for the remote-libvirt provider suite."""

from __future__ import annotations


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
