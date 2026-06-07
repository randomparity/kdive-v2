"""Local-libvirt Discovery plane + Postgres registration bridge (ADR-0023).

`LocalLibvirtDiscovery` enumerates the local libvirt host over an **injected**
connection factory (so unit tests never touch a real host; the real `libvirt.open`
adapter is `live_vm`-only) and advertises arch/cpu/memory, a `gdbstub` transport, and
the per-host concurrent-Allocation cap. `register_local_libvirt_resource` persists the
discovered host as the one `resources` row, idempotently by `(kind, host_uri)`.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RESOURCES
from kdive.domain.allocation_admission import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Resource, ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.providers.interfaces import OwnedInfra, ResourceRecord

_KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
_URI_ENV = "KDIVE_LIBVIRT_URI"
_CAP_ENV = "KDIVE_LIBVIRT_ALLOCATION_CAP"
_DEFAULT_CAP = 1


class _LibvirtDomain(Protocol):
    def name(self) -> str: ...
    def metadata(self, kind: int, uri: str | None, flags: int) -> str: ...


class _LibvirtConn(Protocol):
    def getInfo(self) -> list[Any]: ...
    def getCapabilities(self) -> str: ...
    def listAllDomains(self, flags: int = 0) -> Sequence[_LibvirtDomain]: ...


type Connect = Callable[[], _LibvirtConn]


def _parse_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>`` from the capabilities XML; ``unknown`` if absent.

    Parsed with ``defusedxml`` — the XML crosses a trust boundary (it is emitted by the
    libvirtd process), so entity-expansion DoS (billion-laughs) is neutralized; a
    malformed document returns ``unknown``, an *attack* document raises (fail loud).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def _parse_system_id(meta_xml: str) -> str | None:
    """Read the System uuid from a kdive metadata element; ``None`` if empty/malformed.

    ``defusedxml`` parse (trust boundary, as ``_parse_arch``): malformed → ``None``;
    an attack document raises rather than being silently skipped as "untagged".
    """
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except ET.ParseError:
        return None
    text = (element.text or "").strip()
    return text or None


class LocalLibvirtDiscovery:
    """The realized discovery port for the local libvirt host."""

    def __init__(self, *, host_uri: str, connect: Connect, concurrent_allocation_cap: int) -> None:
        self.host_uri = host_uri
        self._connect = connect
        self.concurrent_allocation_cap = concurrent_allocation_cap

    @classmethod
    def from_env(cls) -> LocalLibvirtDiscovery:
        """Build from ``KDIVE_LIBVIRT_URI`` + ``KDIVE_LIBVIRT_ALLOCATION_CAP`` (default 1).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the cap env var is not an int.
        """
        host_uri = os.environ.get(_URI_ENV, "qemu:///system")
        raw_cap = os.environ.get(_CAP_ENV)
        if raw_cap is None:
            cap = _DEFAULT_CAP
        else:
            try:
                cap = int(raw_cap)
            except ValueError:
                raise CategorizedError(
                    f"{_CAP_ENV}={raw_cap!r} is not an integer",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                ) from None
        # libvirt ships no type stubs; ty infers `virConnect` from its source, which does
        # not structurally match `_LibvirtConn` (invariant return types on the binding's
        # list-returning methods). The connection is duck-typed at the seam — scoped ignore.
        return cls(
            host_uri=host_uri,
            connect=lambda: libvirt.open(host_uri),  # ty: ignore[invalid-argument-type]
            concurrent_allocation_cap=cap,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one `ResourceRecord` for the host (discovery-time id = ``host_uri``)."""
        conn = self._connect()
        info = conn.getInfo()
        capabilities: dict[str, Any] = {
            "arch": _parse_arch(conn.getCapabilities()),
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            CONCURRENT_ALLOCATION_CAP_KEY: self.concurrent_allocation_cap,
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]

    def list_owned(self) -> list[OwnedInfra]:
        """Return `{system_id, domain_name}` for each kdive-tagged domain."""
        conn = self._connect()
        owned: list[OwnedInfra] = []
        for domain in conn.listAllDomains():
            try:
                meta = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, _KDIVE_METADATA_NS, 0)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                    continue  # untagged → not ours
                raise CategorizedError(
                    "libvirt error reading domain metadata",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain.name()},
                ) from exc
            system_id = _parse_system_id(meta)
            if system_id is None:
                continue
            owned.append(OwnedInfra(system_id=system_id, domain_name=domain.name()))
        return owned


async def register_local_libvirt_resource(
    conn: AsyncConnection,
    discovery: LocalLibvirtDiscovery,
    *,
    pool: str,
    cost_class: str,
) -> Resource:
    """Persist the discovered host as the one `resources` row, idempotent by host_uri.

    ``pool`` is the resource pool **name** (``Resource.pool``), not a connection pool. M0
    registers from a single startup/operator path; a ``UNIQUE(kind, host_uri)`` constraint
    is the M1 hardening for concurrent registrars (ADR-0023).
    """
    record = discovery.list_resources()[0]
    capabilities = record["capabilities"]
    kind = record["kind"]
    status = record["status"]
    host_uri = record["resource_id"]
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (kind.value, host_uri),
        )
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE resources SET capabilities = %s, status = %s, pool = %s, "
                "cost_class = %s WHERE id = %s RETURNING *",
                (
                    Jsonb(capabilities),
                    status.value,
                    pool,
                    cost_class,
                    existing["id"],
                ),
            )
            updated = await cur.fetchone()
            if updated is None:  # Invariant: the row was held FOR UPDATE.
                raise RuntimeError("UPDATE of resources returned no row")
            return Resource.model_validate(updated)
    # No existing row: insert via the repository (it wraps capabilities in Jsonb and
    # returns the row with DB-generated timestamps). Runs after the SELECT's transaction
    # commits — acceptable under the M0 single-registrar assumption documented above.
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            kind=kind,
            capabilities=capabilities,
            pool=pool,
            cost_class=cost_class,
            status=status,
            host_uri=host_uri,
        ),
    )


_LOCAL_POOL = "local-libvirt"
_LOCAL_COST_CLASS = "local"


async def ensure_local_host_registered(
    pool: AsyncConnectionPool, *, discovery: LocalLibvirtDiscovery | None = None
) -> None:
    """Register the local-libvirt host as a Resource row **iff absent** (first-run bootstrap).

    Insert-only: the reconciler calls this on every startup, but it registers only when no row
    exists for the host, so a restart never overwrites operator-tuned state — it cannot resurrect
    a drained host to ``available`` or reset a hand-raised ``concurrent_allocation_cap`` to the
    env default (ADR-0059). Without a registered host, ``allocations.request`` has nothing to
    admit against and fails ``configuration_error`` until a row exists. ``discovery`` defaults to
    :meth:`LocalLibvirtDiscovery.from_env`, which reads host capacity from libvirt; tests inject a
    fake. (Single-registrar M0; the ``UNIQUE(kind, host_uri)`` constraint is the M1 hardening for
    a concurrent-registrar race — ADR-0023.)
    """
    disc = discovery if discovery is not None else LocalLibvirtDiscovery.from_env()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM resources WHERE kind = %s AND host_uri = %s",
                (ResourceKind.LOCAL_LIBVIRT.value, disc.host_uri),
            )
            if await cur.fetchone() is not None:
                return  # already registered; leave the operator's status/cap/capabilities intact
        await register_local_libvirt_resource(
            conn, disc, pool=_LOCAL_POOL, cost_class=_LOCAL_COST_CLASS
        )
        await conn.commit()
