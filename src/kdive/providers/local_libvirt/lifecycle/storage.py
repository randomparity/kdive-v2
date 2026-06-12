"""Local-libvirt provisioning storage and console-file lifecycle helpers."""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: S404 - qemu-img is invoked with a fixed argv, no shell
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.runtime_paths import console_log_path

_log = logging.getLogger(__name__)

ROOTFS_DIR = "/var/lib/kdive/rootfs"
_QEMU_IMG_TIMEOUT_S = 5 * 60
_QEMU_IMG = "qemu-img"


def overlay_path(system_id: UUID | str) -> str:
    """The per-System qcow2 overlay path."""
    return f"{ROOTFS_DIR}/{system_id}-overlay.qcow2"


def _real_make_overlay(base: str, overlay: str) -> None:
    """Create the per-System qcow2 overlay backed by ``base`` with ``qemu-img``."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
        )
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted paths
            [qemu_img, "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay],
            capture_output=True,
            text=True,
            timeout=_QEMU_IMG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "qemu-img is not installed; cannot create the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
        ) from exc
    except OSError as exc:
        details = _overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to launch qemu-img to create the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "qemu-img exceeded the overlay creation timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
                "timeout_s": _QEMU_IMG_TIMEOUT_S,
            },
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to create the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={
                **_overlay_error_details("create_overlay", overlay, tool=_QEMU_IMG),
                "stderr": result.stderr[-2000:],
            },
        )


def _real_remove_overlay(overlay: str) -> None:
    """Remove a System's overlay file; an absent file is the achieved post-state."""
    try:
        Path(overlay).unlink(missing_ok=True)
    except OSError as exc:
        details = _overlay_error_details("remove_overlay", overlay)
        details["error"] = type(exc).__name__
        raise CategorizedError(
            "failed to remove the per-System rootfs overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc


def _overlay_error_details(op: str, overlay: str, *, tool: str | None = None) -> dict[str, object]:
    details: dict[str, object] = {"op": op, "overlay": Path(overlay).name}
    if tool is not None:
        details["tool"] = tool
    return details


def _real_overlay_exists(overlay: str) -> bool:
    return Path(overlay).exists()


type MakeOverlay = Callable[[str, str], None]
type RemoveOverlay = Callable[[str], None]
type OverlayExists = Callable[[str], bool]
type PrepareConsoleLog = Callable[[Path], None]


def _prepare_console_log(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644, exist_ok=True)
        path.chmod(0o644)
    except OSError as exc:
        raise CategorizedError(
            "failed to prepare libvirt console log",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"path": str(path)},
        ) from exc


@dataclass(frozen=True, slots=True)
class PreparedOverlay:
    path: str
    created: bool


@dataclass(frozen=True, slots=True)
class ProvisioningFiles:
    make_overlay: MakeOverlay = _real_make_overlay
    remove_overlay: RemoveOverlay = _real_remove_overlay
    overlay_exists: OverlayExists = _real_overlay_exists
    prepare_console_log: PrepareConsoleLog = _prepare_console_log

    def prepare_overlay(self, system_id: UUID, *, base: str) -> PreparedOverlay:
        overlay = overlay_path(system_id)
        created = not self.overlay_exists(overlay)
        if created:
            self.make_overlay(base, overlay)
        return PreparedOverlay(path=overlay, created=created)

    def prepare_console(self, system_id: UUID) -> None:
        self.prepare_console_log(console_log_path(system_id))

    def cleanup_overlay_if_created(self, overlay: PreparedOverlay) -> None:
        if not overlay.created:
            return
        try:
            self.remove_overlay(overlay.path)
        except CategorizedError:
            _log.warning("failed to remove overlay after failed provision", exc_info=True)

    def remove_overlay_for_domain(self, domain_name: str) -> None:
        self.remove_overlay(overlay_path(domain_name.removeprefix("kdive-")))
