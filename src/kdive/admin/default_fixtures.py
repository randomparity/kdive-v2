"""Default fixture catalog files installed by ``python -m kdive install-fixtures``."""

from __future__ import annotations

LOCAL_LIBVIRT_FIXTURES: dict[str, str] = {
    "manifest.yaml": """schema_version: 1
provider: local-libvirt
storage:
  allowed_component_roots:
    - /var/lib/kdive/rootfs
  cache_dir: /var/lib/kdive/rootfs/cache
  overlay_dir: /var/lib/kdive/rootfs/overlays
rootfs:
  - rootfs/fedora-kdive-ready-43.yaml
profiles:
  - profiles/console-ready_x86_64.yaml
""",
    "rootfs/fedora-kdive-ready-43.yaml": """provider: local-libvirt
name: fedora-kdive-ready-43
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: local
  path: /var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2
visibility: public
capabilities:
  - kdive-ready-console
  - ssh
  - drgn
""",
    "profiles/console-ready_x86_64.yaml": """provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
requires:
  config:
    required:
      CONFIG_SERIAL_8250_CONSOLE: y
      CONFIG_VIRTIO_BLK: y
      CONFIG_VIRTIO_PCI: y
  cmdline:
    required_tokens:
      - console=ttyS0
      - root=/dev/vda
    protected_prefixes:
      - console=
      - root=
      - crashkernel=
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities:
      - kdive-ready-console
""",
}
