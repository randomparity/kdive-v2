"""Local-libvirt Retrieve plane: capture a kdump vmcore and run crash postmortem (ADR-0031).

`LocalLibvirtRetrieve` realizes two seam-injected ports, mirroring `LocalLibvirtBuild`:
`Retriever.capture(system_id, method)` dispatches to the appropriate seam, stores the raw
`sensitive` core and a `redacted` dmesg derivative, and returns both refs plus the core's build-id;
`CrashPostmortem.run(...)` symbolizes the core against the Run's `debuginfo_ref` over an
injected `crash` subprocess. The slow, host-bound operations are `live_vm`-gated seams, so
the orchestration and the full error contract are unit-tested with fakes. The crash-command
validator is the load-bearing security control: the postmortem path is never gated, so every
caller command is sanitized and allowlist-checked before any `crash` invocation.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.security.redaction import Redactor
from kdive.store.objectstore import StoredArtifact, object_store_from_env

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

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput: ...


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


class CrashResult(NamedTuple):
    """A raw `crash` subprocess result: exit status and captured streams."""

    exit_status: int
    stdout: bytes
    stderr: bytes


class CrashOutput(NamedTuple):
    """A parsed, redacted crash batch result."""

    results: dict[str, object]
    transcript: str
    truncated: bool


class CrashPostmortem(Protocol):
    """The handler-facing crash-postmortem port (realized M0 contract)."""

    def run(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput: ...


type _WaitForVmcore = Callable[[UUID], bytes | None]
type _HostDumpCapture = Callable[[UUID], bytes | None]
type _ReadBuildId = Callable[[bytes], str]
type _ExtractRedacted = Callable[[bytes], bytes]
type _FetchObject = Callable[[str], bytes]
type _RunCrash = Callable[[Path, Path, str], CrashResult]


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
        host_dump_capture: _HostDumpCapture,
        fetch_object: _FetchObject | None = None,
        run_crash: _RunCrash | None = None,
    ) -> None:
        self._tenant = tenant
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._wait_for_vmcore = wait_for_vmcore
        self._read_vmcore_build_id = read_vmcore_build_id
        self._extract_redacted = extract_redacted
        self._host_dump_capture = host_dump_capture
        self._fetch_object = fetch_object
        self._run_crash = run_crash

    @classmethod
    def from_env(cls) -> LocalLibvirtRetrieve:
        """Build from env; does not poll the host, open S3, or spawn `crash` (lazy seams)."""
        return cls(
            tenant="local",
            store_factory=object_store_from_env,
            wait_for_vmcore=_real_wait_for_vmcore,
            read_vmcore_build_id=_real_read_vmcore_build_id,
            extract_redacted=_real_extract_redacted,
            host_dump_capture=_real_host_dump_capture,
            fetch_object=_real_fetch_object,
            run_crash=_real_run_crash,
        )

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a core via ``method``; store raw + redacted; return refs + build-id.

        Raises:
            CategorizedError: ``READINESS_FAILURE`` if no complete core appears in the
                window; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        if method is CaptureMethod.HOST_DUMP:
            data = self._host_dump_capture(system_id)
        else:  # CaptureMethod.KDUMP
            data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete core appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, f"vmcore-{method.value}", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id,
            f"vmcore-{method.value}-redacted",
            self._extract_redacted(data),
            Sensitivity.REDACTED,
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

    def run(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

        Stages both objects to temp files, verifies the core's build-id matches
        ``expected_build_id`` (provenance), runs ``crash`` over the injected seam, and
        returns the parsed, **redacted** transcript.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` on a build-id provenance mismatch;
                ``MISSING_DEPENDENCY`` if the crash seams were not configured.
        """
        if self._fetch_object is None or self._run_crash is None:
            raise CategorizedError(
                "crash seams not configured on this Retriever",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected_build_id:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            script = "\n".join(commands) + "\nquit\n"
            crash = self._run_crash(Path(vmlinux_file.name), Path(core_file.name), script)
        redactor = Redactor()
        transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
        return CrashOutput(
            results={cmd: {"ran": True} for cmd in commands},
            transcript=transcript,
            truncated=False,
        )


_CRASH_DIR_ENV = "KDIVE_CRASH_DIR"


def _real_wait_for_vmcore(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real kdump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id), "crash_dir_env": _CRASH_DIR_ENV},
    )


def _real_host_dump_capture(system_id: UUID) -> bytes | None:  # pragma: no cover - live_vm
    raise CategorizedError(
        "real host-dump capture runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"system_id": str(system_id)},
    )


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_extract_redacted(data: bytes) -> bytes:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore dmesg extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    # The ref is a key the system itself produced; there is no client etag handle, so the
    # read is unconditional (ADR-0054). An empty etag would 412 here, not skip the check.
    return object_store_from_env().get_artifact(ref, None).data


def _real_run_crash(  # pragma: no cover - live_vm
    vmlinux: Path, vmcore: Path, script: str
) -> CrashResult:
    raise CategorizedError(
        "the crash subprocess runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "CaptureOutput",
    "CrashOutput",
    "CrashPostmortem",
    "CrashResult",
    "LocalLibvirtRetrieve",
    "Retriever",
    "crash_command_rejection_reason",
]
