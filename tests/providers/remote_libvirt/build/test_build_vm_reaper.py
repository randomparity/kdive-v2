"""Unit tests for the ephemeral build-VM reaper helpers (ADR-0100).

The libvirt list/delete I/O is live_vm-gated; these cover the pure domain-name parsing that
drives the reconciler's job-liveness guard, plus the BuildVmReaper protocol conformance and
the disjointness from the System / dump-volume name schemes.
"""

from __future__ import annotations

from uuid import UUID

from kdive.providers.reaping import BuildVmReaper
from kdive.providers.remote_libvirt.build_vm_reaper import (
    RemoteLibvirtBuildVmReaper,
    run_id_from_build_vm_name,
)
from kdive.providers.remote_libvirt.dump_volume_reaper import system_id_from_dump_volume_name
from kdive.providers.remote_libvirt.lifecycle.build_vm import build_domain_name
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry

_RID = UUID("00000000-0000-0000-0000-00000000ca11")


def test_reaper_satisfies_the_build_vm_reaper_port() -> None:
    reaper = RemoteLibvirtBuildVmReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, BuildVmReaper)


def test_run_id_parses_from_the_build_domain_name() -> None:
    assert run_id_from_build_vm_name(build_domain_name(_RID)) == _RID


def test_run_id_is_none_for_non_build_names() -> None:
    # A System domain (kdive-<uuid>) must NOT parse as a build VM (disjoint namespaces).
    assert run_id_from_build_vm_name(domain_name_for(_RID)) is None
    assert run_id_from_build_vm_name("kdive-build-not-a-uuid") is None
    assert run_id_from_build_vm_name("unrelated") is None


def test_build_vm_name_is_not_a_dump_volume_name() -> None:
    # The build-domain marker and the dump-volume marker must be mutually exclusive.
    assert system_id_from_dump_volume_name(build_domain_name(_RID)) is None
