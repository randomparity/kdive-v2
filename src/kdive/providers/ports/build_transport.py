"""Build-host transport provider contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kdive.provider_components.artifacts import PresignedUpload


@dataclass(slots=True, frozen=True)
class CommandResult:
    """The captured result of a remote or local subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str


class BuildTransport(Protocol):
    """Structural port for build-host primitives.

    All methods must be implementable both locally (subprocess + filesystem) and
    remotely (SSH + presigned object-store channels) without altering callers.
    """

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* with a hard *timeout_s* deadline."""
        ...

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Read *path* as raw bytes."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path*, creating or overwriting the file."""
        ...

    def clone(self, remote: str, ref: str, dest: str) -> None:
        """Clone *remote* at *ref* into *dest*."""
        ...

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* via *presigned* and return the ETag."""
        ...

    def cleanup(self, path: str) -> None:
        """Remove *path*, whether directory tree or single file."""
        ...
