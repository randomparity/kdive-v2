"""Provider-neutral worker-side `crash` postmortem over a captured vmcore (ADR-0084).

The worker-side half of the Retrieve plane is identical for every provider: fetch the
core + debuginfo from the object store, verify the core's build-id matches the Run's,
run a validated `crash` command batch over an injected subprocess, and return the
redacted transcript. Lifted out of `local_libvirt/retrieve.py` so `remote_libvirt`
reuses it without a private copy (the ADR-0083 `debug_common` home for shared
worker-side postmortem code). Slow seams (`fetch_object`, `run_crash`, `read_build_id`)
are injected; the defaults are `live_vm`-only.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import CrashOutput, CrashResult
from kdive.security.artifacts.crash_commands import validate_crash_commands
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

type FetchObject = Callable[[str], bytes]
type ReadBuildId = Callable[[bytes], str]
type RunCrash = Callable[[Path, Path, str], CrashResult]


def run_crash_postmortem(
    *,
    vmcore_ref: str,
    debuginfo_ref: str,
    expected_build_id: str,
    commands: list[str],
    fetch_object: FetchObject,
    read_build_id: ReadBuildId,
    run_crash: RunCrash,
    secret_registry: SecretRegistry,
) -> CrashOutput:
    """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a rejected crash command or a
            build-id provenance mismatch; ``STALE_HANDLE`` when a referenced object is
            missing; ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
    """
    rejected = validate_crash_commands(commands)
    if rejected is not None:
        raise CategorizedError(
            "crash command batch rejected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": rejected},
        )
    vmcore_bytes = fetch_object(vmcore_ref)
    observed = read_build_id(vmcore_bytes)
    if observed != expected_build_id:
        raise CategorizedError(
            "captured vmcore build-id does not match the Run's debuginfo build-id",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"vmcore_ref": vmcore_ref},
        )
    vmlinux_bytes = fetch_object(debuginfo_ref)
    with (
        tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
        tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
    ):
        core_file.write(vmcore_bytes)
        core_file.flush()
        vmlinux_file.write(vmlinux_bytes)
        vmlinux_file.flush()
        script = "\n".join(commands) + "\nquit\n"
        crash = run_crash(Path(vmlinux_file.name), Path(core_file.name), script)
    redactor = Redactor(registry=secret_registry)
    transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
    return CrashOutput(
        results={cmd: {"ran": True} for cmd in commands},
        transcript=transcript,
        truncated=False,
    )


def default_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    # The ref is a key the system itself produced; no client etag handle, so the read
    # is unconditional (ADR-0054). A missing object raises STALE_HANDLE in get_artifact.
    return object_store_from_env().get_artifact(ref, None).data


def default_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def default_run_crash(  # pragma: no cover - live_vm
    vmlinux: Path, vmcore: Path, script: str
) -> CrashResult:
    raise CategorizedError(
        "the crash subprocess runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "FetchObject",
    "ReadBuildId",
    "RunCrash",
    "default_fetch_object",
    "default_read_vmcore_build_id",
    "default_run_crash",
    "run_crash_postmortem",
]
