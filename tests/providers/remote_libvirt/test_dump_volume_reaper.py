"""Unit tests for the remote host_dump dump-volume reaper helpers (ADR-0094, #301).

The libvirt I/O is live_vm-gated; these cover the pure name/mtime parsing that drives the
reconciler's live-holder guards, plus the DumpVolumeReaper protocol conformance.
"""

from __future__ import annotations

from uuid import UUID

from kdive.providers.reaping import DumpVolumeReaper
from kdive.providers.remote_libvirt.dump_volume_reaper import (
    RemoteLibvirtDumpVolumeReaper,
    system_id_from_dump_volume_name,
    volume_mtime_epoch_s,
)
from kdive.providers.remote_libvirt.retrieve import host_dump_volume_name
from kdive.security.secrets.secret_registry import SecretRegistry

_SID = UUID("00000000-0000-0000-0000-0000000000cc")


def test_reaper_satisfies_the_dump_volume_reaper_port() -> None:
    reaper = RemoteLibvirtDumpVolumeReaper.from_env(secret_registry=SecretRegistry())
    assert isinstance(reaper, DumpVolumeReaper)


def test_system_id_parses_from_the_deterministic_capture_name() -> None:
    # The reaper must parse exactly what the capture path writes.
    name = host_dump_volume_name(_SID)
    assert system_id_from_dump_volume_name(name) == _SID


def test_system_id_is_none_for_a_non_dump_name() -> None:
    assert system_id_from_dump_volume_name("some-overlay.qcow2") is None
    assert system_id_from_dump_volume_name("kdive-host-dump-not-a-uuid.kdump") is None


def test_mtime_reads_the_target_timestamps_mtime() -> None:
    xml = """
    <volume>
      <name>kdive-host-dump.kdump</name>
      <target>
        <timestamps><mtime>1700000000.123456</mtime></timestamps>
      </target>
    </volume>
    """
    assert volume_mtime_epoch_s(xml) == 1700000000.123456


def test_mtime_is_zero_when_absent_or_malformed() -> None:
    assert volume_mtime_epoch_s("<volume><target/></volume>") == 0.0
    assert volume_mtime_epoch_s("not xml at all <") == 0.0
    assert (
        volume_mtime_epoch_s(
            "<volume><target><timestamps><mtime>nope</mtime></timestamps></target></volume>"
        )
        == 0.0
    )
