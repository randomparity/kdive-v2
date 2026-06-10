"""RemoteLibvirtControl tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import PowerAction
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.control import RemoteLibvirtControl
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_SYSTEM_ID = UUID("00000000-0000-0000-0000-0000000000aa")


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
    )


def _control(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtControl:
    name = domain_name_for(_SYSTEM_ID)
    conn = FakeControlConn({name: domain} if domain is not None else {})
    return RemoteLibvirtControl(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (PowerAction.ON, "create"),
        (PowerAction.OFF, "destroy"),
        (PowerAction.RESET, "reset"),
        (PowerAction.CYCLE, "reboot"),
    ],
)
def test_power_maps_to_libvirt_call(action: PowerAction, expected: str, tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), action)
    assert domain.calls == [expected]


def test_power_on_already_running_swallowed(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"create": libvirt.VIR_ERR_OPERATION_INVALID},
    )
    _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.ON)  # no raise


def test_power_absent_domain_is_control_failure(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as exc:
        _control(None, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.ON)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_power_other_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"reset": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).power(domain_name_for(_SYSTEM_ID), PowerAction.RESET)
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE


def test_force_crash_injects_nmi(tmp_path: Path) -> None:
    domain = FakeDomain(domain_name_for(_SYSTEM_ID))
    _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert domain.calls == ["injectNMI"]


def test_force_crash_libvirt_error_is_control_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name_for(_SYSTEM_ID),
        raise_on={"injectNMI": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    with pytest.raises(CategorizedError) as exc:
        _control(domain, tmp_path).force_crash(domain_name_for(_SYSTEM_ID))
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
