"""Remote-libvirt retrieval provider package."""

from kdive.providers.remote_libvirt.retrieve.facade import (
    RemoteLibvirtRetrieve,
    host_dump_volume_name,
)

__all__ = [
    "RemoteLibvirtRetrieve",
    "host_dump_volume_name",
]
