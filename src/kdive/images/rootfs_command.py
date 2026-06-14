"""CLI assembly for the local `build-fs` filesystem-image build command."""

from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.distros import SUPPORTED_DISTROS, resolve_base_template
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildPlane, RootfsBuildSpec
from kdive.providers.composition import build_local_rootfs_build_plane

_log = logging.getLogger(__name__)

# Today's debug/guest rootfs: the in-target crash + introspection toolchain.
DEFAULT_DEBUG_FS_PACKAGES = ("drgn", "kexec-tools", "makedumpfile")
# A build-host toolchain image: the kernel-build deps a remote/ephemeral build target needs.
DEFAULT_BUILD_FS_PACKAGES = (
    "gcc",
    "make",
    "bc",
    "bison",
    "flex",
    "openssl-devel",
    "elfutils-libelf-devel",
    "ncurses-devel",
    "dwarves",
    "rsync",
    "git",
)
_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"


@dataclass(frozen=True, slots=True)
class _FsKind:
    """The package set and guest-contract capabilities a ``--kind`` selects."""

    packages: tuple[str, ...]
    capabilities: tuple[str, ...]


_FS_KINDS: dict[str, _FsKind] = {
    "debug": _FsKind(DEFAULT_DEBUG_FS_PACKAGES, ("agent", "kdump", "drgn")),
    "build": _FsKind(DEFAULT_BUILD_FS_PACKAGES, ("agent", "build")),
}


def add_build_fs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `build-fs`: the operator's local-libvirt filesystem-image build."""
    build = sub.add_parser(
        "build-fs",
        help="build a local-libvirt kdive-ready filesystem qcow2 (debug guest or build host)",
    )
    build.add_argument(
        "--kind",
        choices=tuple(_FS_KINDS),
        default="debug",
        help="debug = guest crash/introspection rootfs; build = kernel-build-host toolchain image",
    )
    build.add_argument(
        "--distro",
        default="fedora",
        help=f"base-OS family (extensibility seam; implemented: {', '.join(SUPPORTED_DISTROS)})",
    )
    build.add_argument(
        "--workspace",
        default=_DEFAULT_WORKSPACE,
        help=(
            f"build/publish workspace (default: {_DEFAULT_WORKSPACE}); point at a user-writable "
            "path to avoid a privileged mkdir of the root-owned default"
        ),
    )
    build.add_argument(
        "--dest",
        default="/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2",
        help="destination qcow2 path (the produced image is moved here)",
    )
    build.add_argument("--name", default="fedora-kdive-ready-43", help="catalog image name")
    build.add_argument("--arch", default="x86_64")
    build.add_argument("--releasever", default="43", help="release the image is built from")
    build.add_argument(
        "--package",
        action="append",
        default=None,
        dest="packages",
        help="extra guest package (repeatable); defaults to the --kind's package set",
    )


def _build_local_rootfs_plane(workspace: Path) -> RootfsBuildPlane:
    """Resolve the local-libvirt rootfs build plane via the composition seam (test seam)."""
    return build_local_rootfs_build_plane(workspace=workspace)


def _ensure_workspace_writable(workspace: Path) -> None:
    """Create ``workspace`` if absent and verify it is writable, else fail with a fix hint.

    Replaces a bare ``PermissionError`` traceback (the common first-run friction on the
    root-owned default) with an actionable message naming the directory and a command to make
    it writable.
    """
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".build-fs-probe-"):
            pass
    except OSError as exc:
        raise CategorizedError(
            f"build-fs workspace {workspace} is not writable; create it writable first, e.g. "
            f'`sudo install -d -o "$USER" {workspace}`, or pass a writable --workspace',
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"workspace": str(workspace), "error": type(exc).__name__},
        ) from exc


def run_build_fs(args: argparse.Namespace) -> None:
    """Build a kdive-ready filesystem qcow2 via the local plane and move it to ``--dest``."""
    kind = _FS_KINDS[args.kind]
    packages = tuple(args.packages) if args.packages else kind.packages
    source_image_digest = f"virt-builder:{resolve_base_template(args.distro, args.releasever)}"
    spec = RootfsBuildSpec(
        provider="local-libvirt",
        name=args.name,
        arch=args.arch,
        releasever=args.releasever,
        packages=packages,
        source_image_digest=source_image_digest,
        capabilities=kind.capabilities,
        distro=args.distro,
    )
    workspace = Path(args.workspace).resolve()
    _ensure_workspace_writable(workspace)
    plane = _build_local_rootfs_plane(workspace)
    output: RootfsBuildOutput = plane.build(spec)
    dest = Path(args.dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(output.qcow2_path), str(dest))
    dest.chmod(0o644)
    _log.info(
        "built %s rootfs %s digest=%s; set KDIVE_GUEST_IMAGE to this path",
        args.kind,
        dest,
        output.digest,
    )
    print(f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest))}")
