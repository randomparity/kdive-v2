"""Remote-libvirt storage helper tests."""

from __future__ import annotations

from uuid import UUID

import libvirt
import pytest
from defusedxml.ElementTree import fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.storage import (
    PreparedOverlay,
    cleanup_overlay_if_created,
    delete_volume,
    ensure_overlay,
    lookup_pool,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from tests.providers.remote_libvirt.conftest import libvirt_error

_SYSTEM_ID = UUID("00000000-0000-0000-0000-00000000beef")
_BASE_VOLUME = "kdive-base-fedora-42.qcow2"


class _Volume:
    def __init__(
        self,
        name: str,
        *,
        capacity: int = 10 * 2**30,
        pool: _Pool | None = None,
        delete_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.name = name
        self.capacity = capacity
        self.pool = pool
        self.delete_error = delete_error

    def path(self) -> str:
        return f"/pool/{self.name}"

    def info(self) -> list[int]:
        return [0, self.capacity, 0]

    def delete(self, flags: int = 0) -> int:
        del flags
        if self.delete_error is not None:
            raise self.delete_error
        assert self.pool is not None
        self.pool.volumes.pop(self.name, None)
        self.pool.deleted.append(self.name)
        return 0


class _Pool:
    def __init__(
        self,
        volumes: dict[str, _Volume] | None = None,
        *,
        lookup_error: libvirt.libvirtError | None = None,
        create_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.volumes = volumes if volumes is not None else {}
        self.lookup_error = lookup_error
        self.create_error = create_error
        self.created_xml: list[str] = []
        self.deleted: list[str] = []
        for volume in self.volumes.values():
            volume.pool = self

    def storageVolLookupByName(self, name: str) -> _Volume:  # noqa: N802
        if self.lookup_error is not None:
            raise self.lookup_error
        if name in self.volumes:
            return self.volumes[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> _Volume:  # noqa: N802
        del flags
        if self.create_error is not None:
            raise self.create_error
        self.created_xml.append(xml)
        name = fromstring(xml).findtext("./name")
        assert name is not None
        volume = _Volume(name, pool=self)
        self.volumes[name] = volume
        return volume


class _Conn:
    def __init__(
        self,
        pools: dict[str, _Pool] | None = None,
        *,
        lookup_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.pools = pools if pools is not None else {"default": _Pool()}
        self.lookup_error = lookup_error

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        if self.lookup_error is not None:
            raise self.lookup_error
        if name in self.pools:
            return self.pools[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)


def test_lookup_pool_maps_absent_pool_to_configuration_error() -> None:
    with pytest.raises(CategorizedError) as caught:
        lookup_pool(_Conn(), "missing")

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {"pool": "missing"}


def test_lookup_pool_maps_unexpected_libvirt_error_to_infrastructure() -> None:
    with pytest.raises(CategorizedError) as caught:
        lookup_pool(
            _Conn(lookup_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)),
            "default",
        )

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"pool": "default"}


def test_ensure_overlay_reuses_existing_overlay() -> None:
    overlay_name = overlay_volume_name(_SYSTEM_ID)
    pool = _Pool({overlay_name: _Volume(overlay_name)})

    overlay = ensure_overlay(pool, _BASE_VOLUME, _SYSTEM_ID)

    assert overlay == PreparedOverlay(name=overlay_name, created=False)
    assert pool.created_xml == []


def test_ensure_overlay_creates_overlay_from_base_volume() -> None:
    base = _Volume(_BASE_VOLUME, capacity=42)
    pool = _Pool({_BASE_VOLUME: base})

    overlay = ensure_overlay(pool, _BASE_VOLUME, _SYSTEM_ID)

    assert overlay == PreparedOverlay(name=overlay_volume_name(_SYSTEM_ID), created=True)
    [xml] = pool.created_xml
    root = fromstring(xml)
    assert root.findtext("./name") == overlay.name
    assert root.findtext("./capacity") == "42"
    assert root.findtext("./backingStore/path") == "/pool/kdive-base-fedora-42.qcow2"


def test_ensure_overlay_missing_base_volume_is_configuration_error() -> None:
    pool = _Pool()

    with pytest.raises(CategorizedError) as caught:
        ensure_overlay(pool, _BASE_VOLUME, _SYSTEM_ID)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {"base_image_volume": _BASE_VOLUME}


def test_ensure_overlay_create_error_is_provisioning_failure() -> None:
    pool = _Pool(
        {_BASE_VOLUME: _Volume(_BASE_VOLUME)},
        create_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR),
    )

    with pytest.raises(CategorizedError) as caught:
        ensure_overlay(pool, _BASE_VOLUME, _SYSTEM_ID)

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert caught.value.details == {"volume": overlay_volume_name(_SYSTEM_ID)}


def test_cleanup_overlay_only_deletes_created_overlay() -> None:
    overlay_name = overlay_volume_name(_SYSTEM_ID)
    pool = _Pool({overlay_name: _Volume(overlay_name), "existing": _Volume("existing")})

    cleanup_overlay_if_created(pool, PreparedOverlay(overlay_name, created=True))
    cleanup_overlay_if_created(pool, PreparedOverlay("existing", created=False))

    assert overlay_name not in pool.volumes
    assert "existing" in pool.volumes
    assert pool.deleted == [overlay_name]


def test_delete_volume_is_idempotent_for_absent_pool_or_volume() -> None:
    delete_volume(_Conn(), "missing", "overlay")
    delete_volume(_Conn({"default": _Pool()}), "default", "missing")


def test_delete_volume_unexpected_delete_error_is_infrastructure() -> None:
    overlay_name = overlay_volume_name(_SYSTEM_ID)
    pool = _Pool(
        {
            overlay_name: _Volume(
                overlay_name,
                delete_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR),
            )
        }
    )

    with pytest.raises(CategorizedError) as caught:
        delete_volume(_Conn({"default": pool}), "default", overlay_name)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"volume": overlay_name}
