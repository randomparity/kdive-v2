"""CLI assembly for the local rootfs build command."""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

from kdive.images.planes.base import RootfsBuildSpec
from kdive.images.planes.local_libvirt import LocalLibvirtRootfsBuildPlane

_log = logging.getLogger(__name__)

DEFAULT_ROOTFS_PACKAGES = ("drgn", "kexec-tools", "makedumpfile")


def add_build_rootfs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `build-rootfs`: the operator's local-libvirt rootfs build."""
    build = sub.add_parser(
        "build-rootfs", help="build a local-libvirt kdive-ready rootfs qcow2 via the build plane"
    )
    build.add_argument(
        "--dest",
        default="/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2",
        help="destination qcow2 path (the produced image is moved here)",
    )
    build.add_argument("--name", default="fedora-kdive-ready-43", help="catalog image name")
    build.add_argument("--arch", default="x86_64")
    build.add_argument("--releasever", default="43", help="Fedora release the image is built from")
    build.add_argument(
        "--package",
        action="append",
        default=None,
        dest="packages",
        help="extra guest package (repeatable); defaults to drgn,kexec-tools,makedumpfile",
    )


def run_build_rootfs(args: argparse.Namespace) -> None:
    """Build a kdive-ready rootfs qcow2 via the local plane and move it to ``--dest``."""
    packages = tuple(args.packages) if args.packages else DEFAULT_ROOTFS_PACKAGES
    spec = RootfsBuildSpec(
        provider="local-libvirt",
        name=args.name,
        arch=args.arch,
        releasever=args.releasever,
        packages=packages,
        source_image_digest=f"virt-builder:fedora-{args.releasever}",
        capabilities=("agent", "kdump", "drgn"),
    )
    output = LocalLibvirtRootfsBuildPlane.from_env().build(spec)
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(output.qcow2_path), str(dest))
    dest.chmod(0o644)
    _log.info("built rootfs %s digest=%s", dest, output.digest)
