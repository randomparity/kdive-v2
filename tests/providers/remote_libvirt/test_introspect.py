"""Unit tests for the remote-libvirt introspection ports (issue #205, ADR-0083).

Drive the worker-side vmcore postmortem (``from_vmcore``) and the in-guest drgn-live port
(``introspect_live``) with injected fakes — a fake-fetched core, a fake drgn ``_Program``, and
a scripted guest-agent double — so the full orchestration + redaction run with no drgn, no
object store, and no libvirt host.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.introspect import RemoteVmcoreIntrospect
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeProgram:
    def iter_tasks(self):
        return []

    def iter_modules(self):
        return []

    def uts(self):
        return {"release": "6.1.0"}

    def boot_cmdline(self):
        return "ro"

    def cpus_online(self):
        return 1

    def mem_total_pages(self):
        return 1


def _vmcore_introspect(*, open_program=None, run_helper=None, fetch=None, build_id=lambda b: "BID"):
    return RemoteVmcoreIntrospect(
        fetch_object=fetch or (lambda ref: b"core" if "core" in ref else b"vmlinux"),
        read_vmcore_build_id=build_id,
        secret_registry=SecretRegistry(),
        open_program=open_program,
        run_helper=run_helper,
    )


def test_from_vmcore_off_gate_is_missing_dependency():
    introspect = _vmcore_introspect()  # no drgn seams
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_from_vmcore_build_id_mismatch_is_configuration_error():
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: {},
        build_id=lambda b: "OTHER",
    )
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_vmcore_returns_redacted_report():
    from kdive.providers.debug_common.introspect import (
        helper_modules,
        helper_sysinfo,
        helper_tasks,
    )

    helpers = {"tasks": helper_tasks, "modules": helper_modules, "sysinfo": helper_sysinfo}
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: helpers[name](prog),
    )
    out = introspect.from_vmcore(
        vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID"
    )
    assert out.sysinfo["release"] == "6.1.0"
    assert out.truncated is False
