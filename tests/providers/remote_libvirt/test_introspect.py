"""Unit tests for the remote-libvirt introspection ports (issue #205, ADR-0083).

Drive the worker-side vmcore postmortem (``from_vmcore``) and the in-guest drgn-live port
(``introspect_live``) with injected fakes — a fake-fetched core, a fake drgn ``_Program``, and
a scripted guest-agent double — so the full orchestration + redaction run with no drgn, no
object store, and no libvirt host.
"""

from __future__ import annotations

import base64
import json

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLiveIntrospect,
    RemoteVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend


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


_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


class _ScriptedAgent:
    """A qemu_agent_command double implementing the two-phase guest-exec protocol.

    Mirrors test_install.py's scripted agent so the tests exercise the real GuestAgentExec
    (and its worker-side allowlist), not a mock of it. ``handler(argv)`` returns the command's
    AgentExecResult or raises libvirt.libvirtError. Records every argv it ran.
    """

    def __init__(self, handler):
        self._handler = handler
        self._pending = {}
        self._next_pid = 1
        self.argvs: list[list[str]] = []

    def __call__(self, domain, command, timeout, flags):
        payload = json.loads(command)
        if payload["execute"] == "guest-exec":
            args = payload["arguments"]
            argv = [args["path"], *args["arg"]]
            result = self._handler(argv)
            self.argvs.append(argv)
            pid = self._next_pid
            self._next_pid += 1
            self._pending[pid] = result
            return json.dumps({"return": {"pid": pid}})
        if payload["execute"] == "guest-exec-status":
            result = self._pending.pop(payload["arguments"]["pid"])
            return json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": result.exit_status,
                        "out-data": base64.b64encode(result.stdout).decode(),
                        "err-data": base64.b64encode(result.stderr).decode(),
                    }
                }
            )
        raise AssertionError(payload)


class _FakeDomain:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeConn:
    def lookupByName(self, name):  # noqa: N802 - libvirt binding name
        return _FakeDomain(name)

    def close(self):
        pass


def _config_remote():
    return RemoteLibvirtConfig(
        uri="qemu+tls://h/system",
        cert_refs=_REFS,
        concurrent_allocation_cap=1,
        gdb_addr="10.0.0.5",
    )


def _live(agent):
    # RecordingBackend + a real GuestAgentExec run; only the libvirt opener is faked.
    return RemoteLiveIntrospect(
        secret_registry=SecretRegistry(),
        config_factory=_config_remote,
        open_connection=lambda _uri: _FakeConn(),
        agent_command=agent,
        secret_backend_factory=RecordingBackend,
    )


def test_introspect_live_unknown_helper_is_configuration_error():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"{}", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="evil")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.argvs == []  # rejected before any agent round-trip


def test_introspect_live_runs_allowlisted_helper_through_real_guest_agent():
    section = {
        "release": "6.1.0",
        "version": "v",
        "machine": "x86_64",
        "nodename": "n",
        "boot_cmdline": "ro",
        "cpus_online": 1,
        "mem_total_pages": 1,
    }
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent)
    out = live.introspect_live(transport_handle="kdive-sys", helper="sysinfo")
    # the single allowlisted program
    assert agent.argvs == [["/usr/local/sbin/kdive-drgn", "sysinfo"]]
    assert out.sysinfo["release"] == "6.1.0"


def test_introspect_live_nonzero_exit_is_debug_attach_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(1, b"", b"boom"))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="tasks")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_introspect_live_undecodable_output_is_infrastructure_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"not json", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="modules")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
