"""Fault-inject introspection ports."""

from __future__ import annotations

from kdive.providers.ports import IntrospectOutput


class FaultInjectIntrospect:
    """VmcoreIntrospector + LiveIntrospector ports: synthetic introspection output."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)
