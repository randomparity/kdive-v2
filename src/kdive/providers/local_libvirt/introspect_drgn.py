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

from typing import NamedTuple, Protocol


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


__all__ = [
    "IntrospectOutput",
    "VmcoreIntrospector",
]
