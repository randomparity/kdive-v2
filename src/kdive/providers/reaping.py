"""Provider-owned infrastructure reaper contracts."""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class OwnedDomain(Protocol):
    """A provider-owned domain plus its optional kdive System metadata tag."""

    name: str
    system_id: UUID | None


@runtime_checkable
class InfraReaper(Protocol):
    """The narrow provider port the reconciler consumes for leaked infrastructure."""

    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...


class NullReaper:
    """The default reaper: owns nothing, destroys nothing."""

    async def list_owned(self) -> list[OwnedDomain]:
        return []

    async def destroy(self, name: str) -> None:
        return None


class DumpVolume(NamedTuple):
    """A provider's host_dump volume: its name, owning System, and store-side mtime (epoch s).

    ``system_id`` is parsed from the deterministic dump-volume name (ADR-0094); a volume whose
    name does not encode a System is reported with ``system_id=None`` so the reconciler can
    age-reap it without ever skipping it on a (non-existent) live capture.
    """

    name: str
    system_id: UUID | None
    mtime_epoch_s: float


@runtime_checkable
class DumpVolumeReaper(Protocol):
    """The narrow provider port the reconciler consumes for orphaned host_dump volumes.

    Lists the provider's host_dump volumes with their store mtime, and deletes one by name.
    Deletion is idempotent — a volume already gone is not an error (a live capture's own
    ``finally`` may have removed it between the list and the delete).
    """

    async def list_dump_volumes(self) -> list[DumpVolume]: ...
    async def delete_dump_volume(self, name: str) -> None: ...


class NullDumpVolumeReaper:
    """The default dump-volume reaper: owns nothing, deletes nothing."""

    async def list_dump_volumes(self) -> list[DumpVolume]:
        return []

    async def delete_dump_volume(self, name: str) -> None:
        return None


class BuildVm(NamedTuple):
    """A provider's ephemeral build VM: its domain name and the owning Run it was provisioned for.

    ``run_id`` is parsed from the deterministic ``kdive-build-<run_id>`` domain name (ADR-0100); a
    domain whose name does not encode a Run is reported with ``run_id=None`` (the reconciler never
    reaps such a domain, since it cannot confirm the owning build is dead).
    """

    domain_name: str
    run_id: UUID | None


@runtime_checkable
class BuildVmReaper(Protocol):
    """The narrow provider port the reconciler consumes for leaked ephemeral build VMs (ADR-0100).

    Lists the provider's ``kdive-build-*`` domains and deletes one (with its overlay) by name.
    Deletion is idempotent — a domain already gone is not an error.
    """

    async def list_build_vms(self) -> list[BuildVm]: ...
    async def delete_build_vm(self, domain_name: str) -> None: ...


class NullBuildVmReaper:
    """The default build-VM reaper: owns nothing, deletes nothing."""

    async def list_build_vms(self) -> list[BuildVm]:
        return []

    async def delete_build_vm(self, domain_name: str) -> None:
        return None
