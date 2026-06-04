"""Tests for the offline drgn introspection provider (ADR-0033).

The drgn open/helper path is `live_vm`-gated; these tests exercise the orchestration
(provenance, staging, helper dispatch, byte-cap, redaction) against a fake `_Program`
and injected seams — never importing drgn.
"""

from __future__ import annotations

from kdive.providers.local_libvirt.introspect_drgn import (
    IntrospectOutput,
    VmcoreIntrospector,
)


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
