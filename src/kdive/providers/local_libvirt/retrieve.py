"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id)` waits for kdump, stores the raw `sensitive` core and a
`redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run(...)` symbolizes the core against the Run's `debuginfo_ref` over an
injected `crash` subprocess. The slow, host-bound operations are `live_vm`-gated seams, so
the orchestration and the full error contract are unit-tested with fakes. The crash-command
validator is the load-bearing security control: the postmortem path is never gated, so every
caller command is sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import StoredArtifact

# Pipe-to-shell, redirection, command substitution, chaining, backgrounding.
_DENY_CHARS = ("|", ">", "<", "`", "$(", ";", "&")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_RETENTION_CLASS = "vmcore"


def crash_command_rejection_reason(command: str, allowlist: frozenset[str]) -> str | None:
    """Return ``None`` if the command is permitted, else a human-readable rejection reason.

    Two layers: a security-critical denylist (newline/control chars, a leading ``!`` shell
    escape, and the shell metacharacters in ``_DENY_CHARS``) and an allowlist of read-only
    leading verbs. The denylist is the boundary the ungated postmortem path relies on.
    """
    stripped = command.strip()
    if not stripped:
        return "empty command"
    if _CONTROL.search(command):
        return "command contains a newline or control character"
    if stripped[0] == "!":
        return "shell escape ('!') is not permitted"
    for token in _DENY_CHARS:
        if token in command:
            return f"disallowed metacharacter {token!r}"
    verb = stripped.split()[0].lower()
    if verb not in allowlist:
        return f"verb {verb!r} is not in the crash command allowlist"
    return None


class CaptureOutput(NamedTuple):
    """A capture result: the raw + redacted StoredArtifacts and the core's GNU build-id."""

    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str


class Retriever(Protocol):
    """The handler-facing capture port (realized M0 contract), keyed on the System."""

    def capture(self, system_id: UUID) -> CaptureOutput: ...


class _StorePort(Protocol):
    def put_artifact(
        self,
        tenant: str,
        kind: str,
        object_id: str,
        name: str,
        *,
        data: bytes,
        sensitivity: Sensitivity,
        retention_class: str,
    ) -> StoredArtifact: ...


type _WaitForVmcore = Callable[[UUID], bytes | None]
type _ReadBuildId = Callable[[bytes], str]
type _ExtractRedacted = Callable[[bytes], bytes]


class LocalLibvirtRetrieve:
    """The realized Retrieve port: kdump capture + crash postmortem (ADR-0031)."""

    def __init__(
        self,
        *,
        tenant: str,
        store_factory: Callable[[], _StorePort],
        wait_for_vmcore: _WaitForVmcore,
        read_vmcore_build_id: _ReadBuildId,
        extract_redacted: _ExtractRedacted,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._extract_redacted = extract_redacted

    def capture(self, system_id: UUID) -> CaptureOutput:
        """Wait for kdump, store the raw + redacted core, return both refs and the build-id.

        Raises:
            CategorizedError: ``READINESS_FAILURE`` if no complete core appears in the
                window; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete vmcore appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, "vmcore", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id, "vmcore-redacted", self._extract_redacted(data), Sensitivity.REDACTED
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)

    def _put(self, system_id: UUID, name: str, data: bytes, sens: Sensitivity) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            self._tenant,
            "systems",
            str(system_id),
            name,
            data=data,
            sensitivity=sens,
            retention_class=_RETENTION_CLASS,
        )
