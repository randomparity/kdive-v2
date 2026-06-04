"""Offline drgn introspection of a captured vmcore on the host (ADR-0033).

`LocalLibvirtVmcoreIntrospect` realizes the `VmcoreIntrospector` port, mirroring
`LocalLibvirtRetrieve`'s `CrashPostmortem`: fetching the raw core + `vmlinux` from the
object store, verifying the core's build-id against the Run's recorded build-id
(provenance), opening drgn against the staged core, and running three fixed helpers
(tasks, modules, sysinfo). The drgn open/helper path is `live_vm`-gated, so the
orchestration, provenance, dispatch, byte-cap, and redaction are unit-tested with a fake
`_Program`. The assembled report is `Redactor`-scrubbed **inside the port** — the port is
the single redaction boundary, so any later persistence is of already-redacted text.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.redaction import Redactor

# Fixed in-tree caps (no caller args in M0; ADR-0033 §"Output bounds").
_TASK_LIMIT = 200
_BLOCKED_STATES = frozenset({"D"})
_REPORT_BYTE_CAP = 1 << 20  # 1 MiB serialized-report cap; tasks trimmed first.


class _Task(Protocol):
    """The subset of a drgn task object the `tasks` helper reads."""

    def pid(self) -> int: ...
    def tgid(self) -> int: ...
    def comm(self) -> str: ...
    def state(self) -> str: ...
    def kernel_stack(self) -> list[str]: ...


class _Module(Protocol):
    """The subset of a drgn module object the `modules` helper reads."""

    def name(self) -> str: ...
    def size(self) -> int: ...
    def refcount(self) -> int: ...
    def used_by(self) -> list[str]: ...
    def state(self) -> str: ...


class _Program(Protocol):
    """The narrow drgn-program surface the helpers operate on (ADR-0033 §4).

    drgn is confined to the `live_vm`-gated `open_program` seam; the helpers and the tests
    type against this `Protocol` so the rest of the plane is fully ty-checked. The real
    drgn `Program` is adapted to this surface by the `live_vm` seam.
    """

    def iter_tasks(self) -> list[_Task]: ...
    def iter_modules(self) -> list[_Module]: ...
    def uts(self) -> dict[str, str]: ...
    def boot_cmdline(self) -> str: ...
    def cpus_online(self) -> int: ...
    def mem_total_pages(self) -> int: ...


class IntrospectOutput(NamedTuple):
    """A redacted, size-bounded introspection report (ADR-0033 §3/§6).

    The three helper sub-dicts are already `Redactor`-scrubbed when the port returns this.
    ``truncated`` is set when any helper hit its cap or the assembled report hit the byte
    cap (and ``tasks`` was trimmed).
    """

    tasks: dict[str, object]
    modules: dict[str, object]
    sysinfo: dict[str, object]
    truncated: bool


class VmcoreIntrospector(Protocol):
    """The handler-facing offline-introspection port (realized M0 contract)."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput: ...


# --- helpers (M0 subset ported from v1 introspect/helpers/) --------------------------------


def helper_tasks(prog: _Program) -> dict[str, object]:
    """Blocked-task list + kernel stacks (ADR-0033, ported from v1 ``tasks.py``).

    Returns only ``D``-state tasks, bounded by ``_TASK_LIMIT``; ``truncated`` is set when the
    limit is hit. A per-task stack-unwind failure degrades that task's ``kernel_stack`` to a
    marker rather than failing the helper.
    """
    rows: list[dict[str, object]] = []
    truncated = False
    for task in prog.iter_tasks():
        if task.state() not in _BLOCKED_STATES:
            continue
        if len(rows) >= _TASK_LIMIT:
            truncated = True
            break
        rows.append(
            {
                "pid": task.pid(),
                "tgid": task.tgid(),
                "comm": task.comm(),
                "state": task.state(),
                "kernel_stack": _safe_stack(task),
            }
        )
    return {"tasks": rows, "truncated": truncated}


def _safe_stack(task: _Task) -> list[str]:
    try:
        return task.kernel_stack()
    except Exception as exc:  # noqa: BLE001 - offline decode boundary: degrade, never crash the helper
        return [f"<stack unavailable: {type(exc).__name__}>"]


def helper_modules(prog: _Program) -> dict[str, object]:
    """Loaded-module list (ADR-0033, ported from v1 ``modules.py``).

    A per-module decode failure increments ``decode_errors``; an all-failed decode sets
    ``all_failed`` (kernel-version/struct-offset skew) rather than raising, so the call still
    returns a partial report (ADR-0033 §5).
    """
    rows: list[dict[str, object]] = []
    decode_errors = 0
    for module in prog.iter_modules():
        try:
            rows.append(
                {
                    "name": module.name(),
                    "size": module.size(),
                    "refcount": module.refcount(),
                    "used_by": module.used_by(),
                    "state": module.state(),
                }
            )
        except Exception:  # noqa: BLE001 - offline decode boundary: count and continue, never crash
            decode_errors += 1
    all_failed = not rows and decode_errors > 0
    return {"modules": rows, "decode_errors": decode_errors, "all_failed": all_failed}


def helper_sysinfo(prog: _Program) -> dict[str, object]:
    """uts fields, boot cmdline, and basic counters (ADR-0033, ported from v1 ``sysinfo.py``)."""
    uts = prog.uts()
    return {
        "release": uts.get("release", ""),
        "version": uts.get("version", ""),
        "machine": uts.get("machine", ""),
        "nodename": uts.get("nodename", ""),
        "boot_cmdline": prog.boot_cmdline(),
        "cpus_online": prog.cpus_online(),
        "mem_total_pages": prog.mem_total_pages(),
    }


_HELPERS: dict[str, Callable[[_Program], dict[str, object]]] = {
    "tasks": helper_tasks,
    "modules": helper_modules,
    "sysinfo": helper_sysinfo,
}


# --- LocalLibvirtVmcoreIntrospect (the realized port) --------------------------------------

type _FetchObject = Callable[[str], bytes]
type _ReadBuildId = Callable[[bytes], str]
type _OpenProgram = Callable[[Path, Path], _Program]
type _RunHelper = Callable[[_Program, str], dict[str, object]]


class LocalLibvirtVmcoreIntrospect:
    """The realized offline-introspection port (ADR-0033).

    Stages the raw core + ``vmlinux`` from the object store, verifies the core's build-id
    against the Run's recorded build-id (provenance), opens drgn against the staged core
    (``live_vm`` seam), runs the three helpers, redacts and byte-caps the assembled report,
    and returns it — the port is the single redaction boundary.

    The drgn seams (``open_program``/``run_helper``) are ``None`` off-gate; ``from_vmcore``
    then raises ``MISSING_DEPENDENCY`` before touching the store, mirroring
    ``LocalLibvirtRetrieve.run``'s seam guard. The ``live_vm`` runner injects real seams.
    """

    def __init__(
        self,
        *,
        fetch_object: _FetchObject,
        read_vmcore_build_id: _ReadBuildId,
        open_program: _OpenProgram | None = None,
        run_helper: _RunHelper | None = None,
    ) -> None:
        self._fetch_object = fetch_object
        self._read_vmcore_build_id = read_vmcore_build_id
        self._open_program = open_program
        self._run_helper = run_helper
        self._report_byte_cap = _REPORT_BYTE_CAP

    @classmethod
    def from_env(cls) -> LocalLibvirtVmcoreIntrospect:
        """Build from env; does not import drgn or open the store (lazy ``live_vm`` seams).

        The drgn seams are left ``None``, so ``from_vmcore`` raises ``MISSING_DEPENDENCY``
        up front off-gate — it never reads the store or imports drgn. The ``live_vm`` runner
        constructs the port with the real seams injected.
        """
        return cls(
            fetch_object=_real_fetch_object,
            read_vmcore_build_id=_real_read_vmcore_build_id,
        )

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        """Open the core, run the helpers, and return a redacted, size-bounded report.

        Raises:
            CategorizedError: ``MISSING_DEPENDENCY`` if the drgn seams were not configured
                (off-gate); ``CONFIGURATION_ERROR`` on a build-id provenance mismatch;
                ``DEBUG_ATTACH_FAILURE`` if drgn cannot open the core or load the vmlinux.
        """
        if self._open_program is None or self._run_helper is None:
            raise CategorizedError(
                "offline drgn introspection runs only under the live_vm gate",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        vmcore_bytes = self._fetch_object(vmcore_ref)
        self._verify_provenance(vmcore_bytes, expected_build_id, vmcore_ref)
        vmlinux_bytes = self._fetch_object(debuginfo_ref)
        with (
            tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
            tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
        ):
            core_file.write(vmcore_bytes)
            core_file.flush()
            vmlinux_file.write(vmlinux_bytes)
            vmlinux_file.flush()
            program = self._open(self._open_program, Path(core_file.name), Path(vmlinux_file.name))
            tasks = self._run_helper(program, "tasks")
            modules = self._run_helper(program, "modules")
            sysinfo = self._run_helper(program, "sysinfo")
        return self._assemble(tasks, modules, sysinfo)

    def _verify_provenance(self, vmcore_bytes: bytes, expected: str, vmcore_ref: str) -> None:
        observed = self._read_vmcore_build_id(vmcore_bytes)
        if observed != expected:
            raise CategorizedError(
                "captured vmcore build-id does not match the Run's debuginfo build-id",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"vmcore_ref": vmcore_ref},
            )

    @staticmethod
    def _open(open_program: _OpenProgram, core: Path, vmlinux: Path) -> _Program:
        try:
            return open_program(core, vmlinux)
        except CategorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any drgn open fault becomes a typed attach failure
            raise CategorizedError(
                "drgn could not open the vmcore against the supplied vmlinux",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
            ) from exc

    def _assemble(
        self,
        tasks: dict[str, object],
        modules: dict[str, object],
        sysinfo: dict[str, object],
    ) -> IntrospectOutput:
        """Redact first (the single redaction boundary), then byte-cap the redacted report.

        Redaction precedes the byte-cap so the cap bounds the *returned* (redacted) payload
        exactly (ADR-0033 §"Output bounds"), not a pre-redaction size that never ships.
        """
        helper_truncated = bool(tasks.get("truncated"))
        redactor = Redactor()
        tasks = redactor.redact_value(tasks)
        modules = redactor.redact_value(modules)
        sysinfo = redactor.redact_value(sysinfo)
        tasks, byte_trimmed = self._byte_cap(tasks, modules, sysinfo)
        return IntrospectOutput(
            tasks=tasks,
            modules=modules,
            sysinfo=sysinfo,
            truncated=helper_truncated or byte_trimmed,
        )

    def _byte_cap(
        self,
        tasks: dict[str, object],
        modules: dict[str, object],
        sysinfo: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        """Trim the ``tasks`` row list until the serialized report fits the byte cap."""
        raw = tasks.get("tasks")
        rows = list(raw) if isinstance(raw, list) else []
        trimmed = False
        while rows and self._size(rows, modules, sysinfo) > self._report_byte_cap:
            rows = rows[: len(rows) // 2]
            trimmed = True
        return {**tasks, "tasks": rows}, trimmed

    @staticmethod
    def _size(rows: list[object], modules: dict[str, object], sysinfo: dict[str, object]) -> int:
        payload = {"tasks": rows, "modules": modules, "sysinfo": sysinfo}
        return len(json.dumps(payload).encode("utf-8"))


def _real_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env().get_artifact(ref, "").data


def _real_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


__all__ = [
    "IntrospectOutput",
    "LocalLibvirtVmcoreIntrospect",
    "VmcoreIntrospector",
    "helper_modules",
    "helper_sysinfo",
    "helper_tasks",
]
