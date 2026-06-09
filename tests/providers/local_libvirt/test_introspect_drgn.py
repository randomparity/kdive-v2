"""Tests for the offline drgn introspection provider (ADR-0033).

The drgn open/helper path is `live_vm`-gated; these tests exercise the orchestration
(provenance, staging, helper dispatch, byte-cap, redaction) against a fake `_Program`
and injected seams — never importing drgn.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.debug.introspect_drgn import (
    IntrospectOutput,
    LiveIntrospector,
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
    VmcoreIntrospector,
    _Module,
    _Program,
    _Task,
    helper_modules,
    helper_sysinfo,
    helper_tasks,
)


def _rows(out: dict[str, object]) -> list[dict[str, object]]:
    """Narrow a helper's row list for typed subscripting in assertions."""
    return cast("list[dict[str, object]]", out["tasks" if "tasks" in out else "modules"])


class _FakeTask:
    """A canned drgn task; ``raises`` makes ``kernel_stack`` blow up mid-decode."""

    def __init__(
        self,
        pid: int,
        comm: str,
        state: str,
        *,
        raises: bool = False,
        stack: list[str] | None = None,
    ) -> None:
        self._pid = pid
        self._comm = comm
        self._state = state
        self._raises = raises
        self._stack = stack or [f"frame_{pid}"]

    def pid(self) -> int:
        return self._pid

    def tgid(self) -> int:
        return self._pid

    def comm(self) -> str:
        return self._comm

    def state(self) -> str:
        return self._state

    def kernel_stack(self) -> list[str]:
        if self._raises:
            raise RuntimeError("unwind failed")
        return self._stack


class _FakeModule:
    """A canned drgn module; ``raises`` makes ``name`` blow up mid-decode."""

    def __init__(
        self,
        name: str,
        *,
        size: int = 4096,
        refcount: int = 1,
        used_by: list[str] | None = None,
        state: str = "live",
        raises: bool = False,
    ) -> None:
        self._name = name
        self._size = size
        self._refcount = refcount
        self._used_by = used_by or []
        self._state = state
        self._raises = raises

    def name(self) -> str:
        if self._raises:
            raise RuntimeError("bad struct offset")
        return self._name

    def size(self) -> int:
        return self._size

    def refcount(self) -> int:
        return self._refcount

    def used_by(self) -> list[str]:
        return self._used_by

    def state(self) -> str:
        return self._state


class _FakeProgram:
    """A hand-rolled `_Program` with canned tasks/modules/uts for the helper tests."""

    def __init__(
        self,
        *,
        tasks: list[_FakeTask] | None = None,
        modules: list[_FakeModule] | None = None,
        uts: dict[str, str] | None = None,
        boot_cmdline: str = "root=/dev/vda1 quiet",
        cpus_online: int = 4,
        mem_total_pages: int = 1048576,
    ) -> None:
        self._tasks = tasks if tasks is not None else [_FakeTask(1, "init", "D")]
        self._modules = modules if modules is not None else [_FakeModule("nfs")]
        self._uts = uts or {
            "release": "6.8.0",
            "version": "#1 SMP",
            "machine": "x86_64",
            "nodename": "guest",
        }
        self._boot_cmdline = boot_cmdline
        self._cpus_online = cpus_online
        self._mem_total_pages = mem_total_pages

    def iter_tasks(self) -> list[_Task]:
        return list(self._tasks)

    def iter_modules(self) -> list[_Module]:
        return list(self._modules)

    def uts(self) -> dict[str, str]:
        return self._uts

    def boot_cmdline(self) -> str:
        return self._boot_cmdline

    def cpus_online(self) -> int:
        return self._cpus_online

    def mem_total_pages(self) -> int:
        return self._mem_total_pages


def test_introspect_output_has_four_fields() -> None:
    out = IntrospectOutput(
        tasks={"tasks": []}, modules={"modules": []}, sysinfo={"release": "x"}, truncated=False
    )
    assert out.tasks == {"tasks": []}
    assert out.modules == {"modules": []}
    assert out.sysinfo == {"release": "x"}
    assert out.truncated is False


def test_vmcore_introspector_is_protocol() -> None:
    # A minimal duck-typed implementation satisfies the structural protocol.
    class _Impl:
        def from_vmcore(
            self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
        ) -> IntrospectOutput:
            return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    impl: VmcoreIntrospector = _Impl()
    result = impl.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="b")
    assert result.truncated is False


# --- tasks helper --------------------------------------------------------------------------


def test_tasks_filters_blocked_only_and_includes_stack() -> None:
    prog = _FakeProgram(
        tasks=[
            _FakeTask(1, "init", "S"),
            _FakeTask(42, "kworker", "D", stack=["__schedule", "io_schedule"]),
            _FakeTask(7, "running", "R"),
        ]
    )
    out = helper_tasks(prog)
    rows = _rows(out)
    assert [r["pid"] for r in rows] == [42]
    assert rows[0]["kernel_stack"] == ["__schedule", "io_schedule"]
    assert out["truncated"] is False


def test_tasks_respects_limit_and_sets_truncated() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(i, "blocked", "D") for i in range(250)])
    out = helper_tasks(prog)
    rows = _rows(out)
    assert len(rows) == 200
    assert out["truncated"] is True


def test_tasks_stack_decode_failure_degrades_per_row() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(9, "stuck", "D", raises=True)])
    out = helper_tasks(prog)
    rows = _rows(out)
    assert rows[0]["pid"] == 9
    assert rows[0]["kernel_stack"] == ["<stack unavailable: RuntimeError>"]


# --- modules helper ------------------------------------------------------------------------


def test_modules_returns_fields_and_decode_error_count() -> None:
    prog = _FakeProgram(
        modules=[
            _FakeModule("nfs", refcount=3, used_by=["lockd"], state="live"),
            _FakeModule("broken", raises=True),
        ]
    )
    out = helper_modules(prog)
    rows = _rows(out)
    assert rows[0]["name"] == "nfs"
    assert rows[0]["refcount"] == 3
    assert rows[0]["used_by"] == ["lockd"]
    assert out["decode_errors"] == 1
    assert out["all_failed"] is False


def test_modules_all_failed_degrades_not_raises() -> None:
    prog = _FakeProgram(modules=[_FakeModule("a", raises=True), _FakeModule("b", raises=True)])
    out = helper_modules(prog)
    assert out["modules"] == []
    assert out["decode_errors"] == 2
    assert out["all_failed"] is True


def test_modules_monolithic_kernel_is_empty_not_all_failed() -> None:
    prog = _FakeProgram(modules=[])
    out = helper_modules(prog)
    assert out["modules"] == []
    assert out["decode_errors"] == 0
    assert out["all_failed"] is False


# --- sysinfo helper ------------------------------------------------------------------------


def test_sysinfo_returns_uts_and_counters() -> None:
    prog = _FakeProgram()
    out = helper_sysinfo(prog)
    assert out["release"] == "6.8.0"
    assert out["machine"] == "x86_64"
    assert out["boot_cmdline"] == "root=/dev/vda1 quiet"
    assert out["cpus_online"] == 4
    assert out["mem_total_pages"] == 1048576


# --- LocalLibvirtVmcoreIntrospect orchestration --------------------------------------------


def _introspector(
    *,
    program: _FakeProgram | None = None,
    observed_build_id: str = "deadbeef",
    open_raises: CategorizedError | None = None,
) -> LocalLibvirtVmcoreIntrospect:
    """Build an introspector with every seam injected as a fake (no drgn, no store)."""
    prog = program if program is not None else _FakeProgram()

    def _open(vmcore: Path, vmlinux: Path) -> _Program:
        if open_raises is not None:
            raise open_raises
        return prog

    return LocalLibvirtVmcoreIntrospect(
        fetch_object=lambda ref: ref.encode("utf-8"),
        read_vmcore_build_id=lambda data: observed_build_id,
        open_program=_open,
        run_helper=lambda program, name: (
            helper_modules(program)
            if name == "modules"
            else helper_sysinfo(program)
            if name == "sysinfo"
            else helper_tasks(program)
        ),
    )


def test_from_vmcore_happy_path_populates_report() -> None:
    out = _introspector().from_vmcore(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef"
    )
    assert out.sysinfo["release"] == "6.8.0"
    assert cast("list[object]", out.tasks["tasks"])  # the canned blocked task is present
    assert out.truncated is False


def test_from_vmcore_build_id_mismatch_is_configuration_error() -> None:
    introspector = _introspector(observed_build_id="0ther")
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_vmcore_open_failure_is_debug_attach_failure() -> None:
    boom = CategorizedError("drgn cannot open", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
    introspector = _introspector(open_raises=boom)
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_from_vmcore_redacts_guest_strings_at_the_port_boundary() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(13, "token=hunter2", "D")])
    out = _introspector(program=prog).from_vmcore(
        vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef"
    )
    rows = cast("list[dict[str, object]]", out.tasks["tasks"])
    assert "hunter2" not in str(rows)
    assert "[REDACTED]" in str(rows)


def test_from_vmcore_byte_cap_trims_tasks_and_sets_truncated() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(i, "blocked", "D", stack=["x" * 64]) for i in range(200)])
    introspector = _introspector(program=prog)
    # A tiny cap forces trimming of the (capped-at-200) tasks list.
    introspector._report_byte_cap = 256
    out = introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert out.truncated is True
    trimmed = cast("list[object]", out.tasks["tasks"])
    assert len(trimmed) < 200


def test_from_env_real_seams_raise_missing_dependency() -> None:
    introspector = LocalLibvirtVmcoreIntrospect.from_env()
    with pytest.raises(CategorizedError) as exc:
        introspector.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="deadbeef")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


# --- LocalLibvirtLiveIntrospect orchestration (ADR-0039) -----------------------------------


def _live_introspector(
    *,
    program: _FakeProgram | None = None,
    open_raises: CategorizedError | None = None,
) -> LocalLibvirtLiveIntrospect:
    """Build a live introspector with the open-live-program seam injected as a fake."""
    prog = program if program is not None else _FakeProgram()

    def _open_live(transport_handle: str) -> _Program:
        if open_raises is not None:
            raise open_raises
        return prog

    return LocalLibvirtLiveIntrospect(
        open_live_program=_open_live,
        run_helper=lambda program, name: (
            helper_modules(program)
            if name == "modules"
            else helper_sysinfo(program)
            if name == "sysinfo"
            else helper_tasks(program)
        ),
    )


def test_live_introspector_is_protocol() -> None:
    class _Impl:
        def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
            return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    impl: LiveIntrospector = _Impl()
    assert (
        impl.introspect_live(transport_handle="ssh://127.0.0.1:22", helper="tasks").truncated
        is False
    )


def test_run_happy_path_runs_selected_helper() -> None:
    out = _live_introspector().introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="tasks"
    )
    assert cast("list[object]", out.tasks["tasks"])  # the canned blocked task is present
    assert out.modules == {}
    assert out.sysinfo == {}
    assert out.truncated is False


def test_run_selected_helper_only_runs_that_helper() -> None:
    calls: list[str] = []

    def _run_helper(program: _Program, name: str) -> dict[str, object]:
        calls.append(name)
        return {
            "tasks": helper_tasks,
            "modules": helper_modules,
            "sysinfo": helper_sysinfo,
        }[name](program)

    introspector = LocalLibvirtLiveIntrospect(
        open_live_program=lambda _handle: _FakeProgram(),
        run_helper=_run_helper,
    )
    out = introspector.introspect_live(
        transport_handle="ssh://127.0.0.1:22",
        helper="modules",
    )
    assert calls == ["modules"]
    assert out.tasks == {"tasks": []}
    assert "modules" in out.modules
    assert out.sysinfo == {}


def test_run_open_failure_is_debug_attach_failure() -> None:
    boom = CategorizedError("drgn cannot attach", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
    with pytest.raises(CategorizedError) as exc:
        _live_introspector(open_raises=boom).introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks"
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_run_transport_failure_propagates_typed() -> None:
    boom = CategorizedError("ssh dropped", category=ErrorCategory.TRANSPORT_FAILURE)
    with pytest.raises(CategorizedError) as exc:
        _live_introspector(open_raises=boom).introspect_live(
            transport_handle="ssh://127.0.0.1:22", helper="tasks"
        )
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_run_arbitrary_open_error_becomes_debug_attach_failure() -> None:
    # A non-categorized fault from the live seam is normalized to an attach failure (as offline).
    def _open_live(_handle: str) -> _Program:
        raise RuntimeError("kcore permission denied")

    introspector = LocalLibvirtLiveIntrospect(
        open_live_program=_open_live, run_helper=lambda p, n: helper_tasks(p)
    )
    with pytest.raises(CategorizedError) as exc:
        introspector.introspect_live(transport_handle="ssh://127.0.0.1:22", helper="tasks")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_run_redacts_guest_strings_at_the_port_boundary() -> None:
    prog = _FakeProgram(tasks=[_FakeTask(13, "token=hunter2", "D")])
    out = _live_introspector(program=prog).introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="tasks"
    )
    rows = cast("list[dict[str, object]]", out.tasks["tasks"])
    assert "hunter2" not in str(rows)
    assert "[REDACTED]" in str(rows)


def test_run_modules_decode_skew_degrades_not_raises() -> None:
    prog = _FakeProgram(modules=[_FakeModule("a", raises=True), _FakeModule("b", raises=True)])
    out = _live_introspector(program=prog).introspect_live(
        transport_handle="ssh://127.0.0.1:22", helper="modules"
    )
    assert out.modules["all_failed"] is True
    assert out.modules["modules"] == []


def test_live_from_env_real_seam_raises_missing_dependency() -> None:
    introspector = LocalLibvirtLiveIntrospect.from_env()
    with pytest.raises(CategorizedError) as exc:
        introspector.introspect_live(transport_handle="ssh://127.0.0.1:22", helper="tasks")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
