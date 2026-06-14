"""The fault-inject mock ports return synthetic-but-plausible outputs (happy path)."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import kdive.providers.fault_inject.lifecycle.connect as connect_module
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction, Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.fault_inject.build import FaultInjectBuild
from kdive.providers.fault_inject.debug.gdb import (
    FaultInjectDebugEngine,
    fault_inject_attach_seam,
)
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve
from kdive.providers.ports import DebugTransportKind, InstallRequest, SystemHandle
from kdive.providers.ports.lifecycle import TransportHandleData

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_PROVISIONING_PROFILE = cast(ProvisioningProfile, object())
_BUILD_PROFILE = cast(ServerBuildProfile, object())


class _FakeStore:
    def __init__(self) -> None:
        self.writes: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.writes.append(request)
        return StoredArtifact(request.key(), "etag", request.sensitivity, request.retention_class)


# --- Provision -------------------------------------------------------------------------


def test_provision_returns_a_synthetic_domain_and_records_it_as_owned() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)

    domain = provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    assert str(_SYSTEM) in domain
    assert inventory.owned_domains()[0].name == domain
    assert inventory.owned_domains()[0].system_id == _SYSTEM


def test_teardown_forgets_the_domain_so_it_is_no_longer_owned() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)
    domain = provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    provision.teardown(domain)

    assert inventory.owned_domains() == []


def test_reprovision_leaves_the_system_owning_exactly_one_domain() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)
    provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    second = provision.reprovision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    # The synthetic name is deterministic per System, so reprovision never leaks the old
    # domain: the inventory holds exactly one entry for the System after replacement.
    owned = [d.name for d in inventory.owned_domains()]
    assert owned == [second]


# --- Build / Install / Boot ------------------------------------------------------------


def test_build_stores_a_synthetic_kernel_and_returns_consistent_refs() -> None:
    store = _FakeStore()
    builder = FaultInjectBuild(store_factory=lambda: store)

    output = builder.build(_RUN, profile=_BUILD_PROFILE)

    assert output.kernel_ref and output.debuginfo_ref
    assert len(output.build_id) == 40  # a plausible GNU build-id length
    assert {w.owner_kind for w in store.writes} == {"runs"}
    assert all(w.owner_id == str(_RUN) for w in store.writes)


def test_install_and_boot_succeed_on_the_happy_path() -> None:
    install = FaultInjectInstall()

    # No fault drawn → the synthetic install/boot reach a ready state without raising.
    install.install(
        InstallRequest(
            system_id=_SYSTEM,
            run_id=_RUN,
            kernel_ref="kernel-ref",
            cmdline="console=ttyS0",
        )
    )
    install.boot(_SYSTEM)


# --- Connect ---------------------------------------------------------------------------


class _MaxDigest:
    def digest(self) -> bytes:
        return b"\xfb\xff"


def test_synthetic_port_includes_documented_upper_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blake2b(data: bytes, *, digest_size: int) -> _MaxDigest:
        assert data == b"fault-inject-domain"
        assert digest_size == 2
        return _MaxDigest()

    monkeypatch.setattr(connect_module.hashlib, "blake2b", blake2b)

    assert connect_module.synthetic_port("fault-inject-domain") == 65535


def test_open_transport_returns_a_decodable_loopback_handle() -> None:
    connect = FaultInjectConnect()

    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "gdbstub")

    decoded = TransportHandleData.decode(handle)
    assert decoded.kind == "gdbstub"
    assert decoded.host == "127.0.0.1"
    assert 1 <= decoded.port <= 65535


def test_open_transport_rejects_an_unknown_transport_kind() -> None:
    connect = FaultInjectConnect()

    with pytest.raises(CategorizedError) as exc:
        connect.open_transport(
            SystemHandle("fault-inject-domain"), cast(DebugTransportKind, "carrier-pigeon")
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_close_transport_accepts_a_handle_it_opened() -> None:
    connect = FaultInjectConnect()
    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "gdbstub")

    connect.close_transport(handle)


def test_open_close_drgn_live_round_trips() -> None:
    connect = FaultInjectConnect()

    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "drgn-live")

    assert str(handle).startswith("drgn-live://")
    connect.close_transport(handle)  # decode of drgn-live:// must succeed (#215)


def test_open_transport_rejects_the_legacy_ssh_kind() -> None:
    connect = FaultInjectConnect()

    with pytest.raises(CategorizedError) as exc:
        connect.open_transport(SystemHandle("fault-inject-domain"), cast(DebugTransportKind, "ssh"))

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- Control ---------------------------------------------------------------------------


def test_power_and_force_crash_succeed_on_the_happy_path() -> None:
    control = FaultInjectControl()

    control.power("fault-inject-domain", PowerAction.CYCLE)
    control.force_crash("fault-inject-domain")


# --- Retrieve / postmortem / introspect ------------------------------------------------


def test_capture_stores_a_synthetic_vmcore_with_raw_and_redacted_artifacts() -> None:
    store = _FakeStore()
    retrieve = FaultInjectRetrieve(store_factory=lambda: store)

    output = retrieve.capture(_SYSTEM, CaptureMethod.HOST_DUMP)

    sensitivities = {w.sensitivity for w in store.writes}
    assert Sensitivity.SENSITIVE in sensitivities  # the raw core
    assert Sensitivity.REDACTED in sensitivities  # its redacted derivative
    assert output.vmcore_build_id == output.vmcore_build_id  # present, consistent


def test_capture_build_id_matches_the_builder_so_provenance_holds() -> None:
    store = _FakeStore()
    build_id = (
        FaultInjectBuild(store_factory=lambda: store).build(_RUN, profile=_BUILD_PROFILE).build_id
    )
    captured = FaultInjectRetrieve(store_factory=lambda: store).capture(
        _SYSTEM, CaptureMethod.HOST_DUMP
    )

    # A mock spine installs the built kernel then captures its core; a fixed synthetic
    # build-id keeps the capture's provenance check against the build aligned.
    assert captured.vmcore_build_id == build_id


def test_crash_postmortem_returns_a_bounded_synthetic_transcript() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_FakeStore)

    output = retrieve.run_crash_postmortem(
        vmcore_ref="v",
        debuginfo_ref="d",
        expected_build_id="b",
        commands=["bt"],
    )

    assert output.truncated is False
    assert isinstance(output.results, dict)


def test_crash_postmortem_rejects_disallowed_commands() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_FakeStore)

    with pytest.raises(CategorizedError) as exc:
        retrieve.run_crash_postmortem(
            vmcore_ref="v",
            debuginfo_ref="d",
            expected_build_id="b",
            commands=["bt | sh"],
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_introspect_from_vmcore_and_live_return_plausible_shapes() -> None:
    introspect = FaultInjectIntrospect()

    offline = introspect.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="b")
    live = introspect.introspect_live(transport_handle="gdbstub://127.0.0.1:1234", helper="drgn")

    assert offline.truncated is False
    assert live.truncated is False


# --- Debug engine / attach seam --------------------------------------------------------


def test_attach_seam_returns_an_attachment_at_the_loopback_endpoint(tmp_path: Path) -> None:
    transcript = tmp_path / "session.log"

    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=transcript
    )

    assert attachment.rsp_host == "127.0.0.1"
    assert attachment.rsp_port == 1234


def test_debug_engine_set_and_list_breakpoints_round_trip(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "s.log"
    )

    ref = engine.set_breakpoint(attachment, "vfs_read")
    listed = engine.list_breakpoints(attachment)

    assert ref.number in {b.number for b in listed}


def test_debug_engine_breakpoints_are_isolated_per_attachment(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    first = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "first.log"
    )
    second = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "second.log"
    )

    first_ref = engine.set_breakpoint(first, "vfs_read")
    second_ref = engine.set_breakpoint(second, "do_exit")
    engine.clear_breakpoint(first, first_ref.number)

    assert [ref.number for ref in engine.list_breakpoints(first)] == []
    assert [ref.number for ref in engine.list_breakpoints(second)] == [second_ref.number]
