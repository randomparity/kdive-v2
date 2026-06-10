"""Provider-neutral drgn report helpers + redact/byte-cap assembly (ADR-0033/0083).

The three fixed helpers (tasks, modules, sysinfo), the narrow drgn ``_Program`` surface they
operate on, and ``assemble_report`` (redact first — the single redaction boundary — then
byte-cap) are shared by every provider's vmcore and live introspectors. The real drgn
``Program`` is adapted to ``_Program`` by each provider's ``live_vm`` seam.
"""

from __future__ import annotations

import json
from typing import Protocol

from kdive.providers.ports import IntrospectOutput
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

# Fixed in-tree caps (no caller args; ADR-0033 §"Output bounds").
_TASK_LIMIT = 200
_BLOCKED_STATES = frozenset({"D"})
_REPORT_BYTE_CAP = 1 << 20  # 1 MiB serialized-report cap; tasks trimmed first.


class _Task(Protocol):
    """The subset of a drgn task object the ``tasks`` helper reads."""

    def pid(self) -> int: ...
    def tgid(self) -> int: ...
    def comm(self) -> str: ...
    def state(self) -> str: ...
    def kernel_stack(self) -> list[str]: ...


class _Module(Protocol):
    """The subset of a drgn module object the ``modules`` helper reads."""

    def name(self) -> str: ...
    def size(self) -> int: ...
    def refcount(self) -> int: ...
    def used_by(self) -> list[str]: ...
    def state(self) -> str: ...


class _Program(Protocol):
    """The narrow drgn-program surface the helpers operate on (ADR-0033 §4).

    drgn is confined to each provider's ``live_vm``-gated open seam; the helpers and the tests
    type against this ``Protocol`` so the rest of the plane is fully ty-checked. The real drgn
    ``Program`` is adapted to this surface by the ``live_vm`` seam.
    """

    def iter_tasks(self) -> list[_Task]: ...
    def iter_modules(self) -> list[_Module]: ...
    def uts(self) -> dict[str, str]: ...
    def boot_cmdline(self) -> str: ...
    def cpus_online(self) -> int: ...
    def mem_total_pages(self) -> int: ...


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


def assemble_report(
    tasks: dict[str, object],
    modules: dict[str, object],
    sysinfo: dict[str, object],
    *,
    byte_cap: int,
    secret_registry: SecretRegistry,
) -> IntrospectOutput:
    """Redact first (the single redaction boundary), then byte-cap the redacted report.

    Shared by the offline and live ports. Redaction precedes the byte-cap so the cap bounds
    the *returned* (redacted) payload exactly (ADR-0033 §"Output bounds"), not a
    pre-redaction size that never ships.
    """
    helper_truncated = bool(tasks.get("truncated"))
    redactor = Redactor(registry=secret_registry)
    tasks = redactor.redact_value(tasks)
    modules = redactor.redact_value(modules)
    sysinfo = redactor.redact_value(sysinfo)
    tasks, byte_trimmed = _byte_cap(tasks, modules, sysinfo, byte_cap=byte_cap)
    return IntrospectOutput(
        tasks=tasks,
        modules=modules,
        sysinfo=sysinfo,
        truncated=helper_truncated or byte_trimmed,
    )


def _byte_cap(
    tasks: dict[str, object],
    modules: dict[str, object],
    sysinfo: dict[str, object],
    *,
    byte_cap: int,
) -> tuple[dict[str, object], bool]:
    """Trim the ``tasks`` row list until the serialized report fits the byte cap."""
    raw = tasks.get("tasks")
    rows = list(raw) if isinstance(raw, list) else []
    trimmed = False
    while rows and _report_size(rows, modules, sysinfo) > byte_cap:
        rows = rows[: len(rows) // 2]
        trimmed = True
    return {**tasks, "tasks": rows}, trimmed


def _report_size(rows: list[object], modules: dict[str, object], sysinfo: dict[str, object]) -> int:
    payload = {"tasks": rows, "modules": modules, "sysinfo": sysinfo}
    return len(json.dumps(payload).encode("utf-8"))


__all__ = [
    "assemble_report",
    "helper_modules",
    "helper_sysinfo",
    "helper_tasks",
]
