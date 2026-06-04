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

from collections.abc import Callable
from typing import NamedTuple, Protocol

# Fixed in-tree caps (no caller args in M0; ADR-0033 §"Output bounds").
_TASK_LIMIT = 200
_BLOCKED_STATES = frozenset({"D"})


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


__all__ = [
    "IntrospectOutput",
    "VmcoreIntrospector",
    "helper_modules",
    "helper_sysinfo",
    "helper_tasks",
]
