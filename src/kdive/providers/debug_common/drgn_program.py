"""Real drgn-backed seams for the worker-side vmcore introspection ports (ADR-0033/0083).

The introspection ports keep drgn behind injected ``open_program``/``run_helper`` seams so
unit tests never import it. These are the production implementations: drgn is imported
lazily inside the seam, so composition still builds on hosts without it and the port
surfaces the documented ``MISSING_DEPENDENCY`` instead of an ``ImportError``.

``read_vmcoreinfo_build_id`` reads the crashed kernel's GNU build-id from the VMCOREINFO
note (its ``BUILD-ID=`` line, present since v5.13) rather than from ELF section notes —
a kdump core carries VMCOREINFO but not the kernel image's own ``.notes`` section.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.introspect import (
    helper_modules,
    helper_sysinfo,
    helper_tasks,
)

# VMCOREINFO sits in a PT_NOTE near the start of the core; bound the scan so a
# pathological core cannot make provenance verification quadratic.
_VMCOREINFO_SCAN_BYTES = 64 * 1024 * 1024
_BUILD_ID_LINE = re.compile(rb"BUILD-ID=([0-9a-f]{40})")


def read_vmcoreinfo_build_id(data: bytes) -> str:
    """The crashed kernel's GNU build-id from the core's VMCOREINFO ``BUILD-ID=`` line.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no build-id line is present —
            an ELF-format kdump core always carries VMCOREINFO, so its absence means
            the capture path produced something this platform cannot verify.
    """
    match = _BUILD_ID_LINE.search(data[:_VMCOREINFO_SCAN_BYTES])
    if match is None:
        raise CategorizedError(
            "vmcore carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def _require_drgn() -> Any:
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; offline introspection needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    return drgn


class _DrgnTask:
    def __init__(self, prog: Any, task: Any) -> None:
        self._prog = prog
        self._task = task

    def pid(self) -> int:
        return int(self._task.pid)

    def tgid(self) -> int:
        return int(self._task.tgid)

    def comm(self) -> str:
        return self._task.comm.string_().decode("utf-8", "replace")

    def state(self) -> str:
        from drgn.helpers.linux import sched  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return sched.task_state_to_char(self._task)

    def kernel_stack(self) -> list[str]:
        trace = self._prog.stack_trace(self._task)
        return [str(frame) for frame in trace]


class _DrgnModule:
    def __init__(self, module: Any) -> None:
        self._module = module

    def name(self) -> str:
        return self._module.name.string_().decode("utf-8", "replace")

    def size(self) -> int:
        try:
            return int(self._module.mem[0].size)
        except Exception:  # noqa: BLE001 - layout varies by kernel; size is advisory
            return 0

    def refcount(self) -> int:
        return int(self._module.refcnt.counter)

    def used_by(self) -> list[str]:
        return []

    def state(self) -> str:
        return str(int(self._module.state))


class DrgnProgramAdapter:
    """Adapt a ``drgn.Program`` to the introspection helpers' ``_Program`` protocol."""

    def __init__(self, prog: Any) -> None:
        self._prog = prog

    def iter_tasks(self) -> list[object]:
        from drgn.helpers.linux import pid  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return [_DrgnTask(self._prog, task) for task in pid.for_each_task(self._prog)]

    def iter_modules(self) -> list[object]:
        from drgn.helpers.linux import module  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return [_DrgnModule(mod) for mod in module.for_each_module(self._prog)]

    def uts(self) -> dict[str, str]:
        name = self._prog["init_uts_ns"].name
        return {
            "release": name.release.string_().decode("utf-8", "replace"),
            "version": name.version.string_().decode("utf-8", "replace"),
            "machine": name.machine.string_().decode("utf-8", "replace"),
            "nodename": name.nodename.string_().decode("utf-8", "replace"),
        }

    def boot_cmdline(self) -> str:
        return self._prog["saved_command_line"].string_().decode("utf-8", "replace")

    def cpus_online(self) -> int:
        from drgn.helpers.linux import cpumask  # noqa: PLC0415  # ty: ignore[unresolved-import]

        return sum(1 for _ in cpumask.for_each_online_cpu(self._prog))

    def mem_total_pages(self) -> int:
        try:
            return int(self._prog["_totalram_pages"].counter)
        except Exception:  # noqa: BLE001 - symbol name varies by version; advisory counter
            return 0


def open_vmcore_program(core: Path, vmlinux: Path) -> DrgnProgramAdapter:
    """Open a drgn program over a staged vmcore + vmlinux pair (the ``open_program`` seam)."""
    drgn = _require_drgn()
    prog = drgn.Program()
    prog.set_core_dump(core)
    prog.load_debug_info([vmlinux])
    return DrgnProgramAdapter(prog)


def run_introspection_helper(program: Any, name: str) -> dict[str, object]:
    """Dispatch one fixed helper by name (the ``run_helper`` seam)."""
    helpers = {"tasks": helper_tasks, "modules": helper_modules, "sysinfo": helper_sysinfo}
    try:
        helper = helpers[name]
    except KeyError:
        raise CategorizedError(
            f"unknown introspection helper: {name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    return helper(program)
