"""Fakes + fixtures for the local-libvirt discovery tests.

`FakeLibvirtConn` returns canned host info / capabilities XML / domains so discovery is
covered without a real libvirt host (no `live_vm`). The Postgres fixtures are re-exported
for the registration test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libvirt

from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401

_CAPS_XML = "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"


def libvirt_error(code: int) -> libvirt.libvirtError:
    """Build a libvirtError whose get_error_code() returns ``code``."""
    err = libvirt.libvirtError("synthetic")
    # get_error_code() reads self.err[0]; libvirtError leaves err=None with no live error.
    err.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


@dataclass
class FakeDomain:
    domain_name: str
    system_id: str | None  # None → no kdive metadata (raises VIR_ERR_NO_DOMAIN_METADATA)
    raise_code: int | None = None  # override: raise a libvirtError with this code

    def name(self) -> str:
        return self.domain_name

    def metadata(self, kind: int, uri: str | None, flags: int) -> str:
        if self.raise_code is not None:
            raise libvirt_error(self.raise_code)
        if self.system_id is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)
        return f'<kdive:system xmlns:kdive="{uri}">{self.system_id}</kdive:system>'


@dataclass
class FakeLibvirtConn:
    caps_xml: str = _CAPS_XML
    info: list[object] = field(default_factory=lambda: ["x86_64", 16384, 8, 2400, 1, 1, 4, 2])
    domains: list[FakeDomain] = field(default_factory=list)

    def getInfo(self) -> list[object]:
        return self.info

    def getCapabilities(self) -> str:
        return self.caps_xml

    def listAllDomains(self, flags: int = 0) -> list[FakeDomain]:
        return self.domains
