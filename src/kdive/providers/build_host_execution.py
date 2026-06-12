"""Build-host subprocess execution and artifact reader helpers."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - all calls use fixed argv and no shell
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.build_validation import parse_gnu_build_id

MAKE_TIMEOUT_S = 2 * 60 * 60
OBJCOPY_TIMEOUT_S = 60

type ReadConfig = Callable[[Path], str]
type RunStep = Callable[[Path], int]
type RunModulesInstall = Callable[[Path, Path], int]
type ReadBytes = Callable[[Path], bytes]
type ReadBuildId = Callable[[Path], str]


def read_text_file(path: Path, *, category: ErrorCategory, file_label: str) -> str:
    """Read text or raise a categorized unreadable-file error."""
    try:
        return path.read_text()
    except OSError as exc:
        raise CategorizedError(
            f"{file_label} is missing or unreadable",
            category=category,
            details={"file": file_label},
        ) from exc


def read_bytes_file(path: Path, *, category: ErrorCategory, output: str) -> bytes:
    """Read bytes or raise a categorized unreadable-output error."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CategorizedError(
            f"{output} is missing or unreadable",
            category=category,
            details={"output": output},
        ) from exc


def launch_failure(tool: str, exc: OSError, *, category: ErrorCategory) -> CategorizedError:
    """Map a subprocess launch failure into the provider error taxonomy."""
    if isinstance(exc, FileNotFoundError):
        return CategorizedError(
            f"{tool} is required for kernel builds",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": tool},
        )
    return CategorizedError(
        f"{tool} failed to launch",
        category=category,
        details={"tool": tool, "op": "launch"},
    )


def workspace_failure(op: str, path_label: str, exc: OSError) -> CategorizedError:
    """Map workspace filesystem failures into infrastructure failures."""
    return CategorizedError(
        f"build workspace {op} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": op, "path": path_label},
    )


def real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    return read_text_file(
        workspace / ".config",
        category=ErrorCategory.CONFIGURATION_ERROR,
        file_label=".config",
    )


def real_read_kernel_image(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return read_bytes_file(
        workspace / "arch/x86/boot/bzImage",
        category=ErrorCategory.BUILD_FAILURE,
        output="bzImage",
    )


def real_read_vmlinux(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return read_bytes_file(
        workspace / "vmlinux",
        category=ErrorCategory.BUILD_FAILURE,
        output="vmlinux",
    )


def real_run_make(workspace: Path) -> int:  # pragma: no cover - live_vm
    """Run the default parallel kernel build."""
    try:
        return subprocess.run(
            ["make", "-C", str(workspace), f"-j{os.cpu_count() or 1}"],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def real_run_olddefconfig(workspace: Path) -> int:  # pragma: no cover - live_vm
    return run_make_target(workspace, ["olddefconfig"], "make olddefconfig")


def real_run_modules_install(workspace: Path, mod_root: Path) -> int:  # pragma: no cover
    return run_make_target(
        workspace,
        [f"INSTALL_MOD_PATH={mod_root}", "modules_install"],
        "make modules_install",
    )


def run_make_target(workspace: Path, args: list[str], label: str) -> int:
    """Run ``make -C <workspace> <args...>`` and map launch/timeout faults."""
    try:
        return subprocess.run(
            ["make", "-C", str(workspace), *args],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{label} exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux`` GNU build-id from its merged ``.notes`` section."""
    with tempfile.NamedTemporaryFile(suffix=".note") as note_file:
        try:
            subprocess.run(
                [
                    "objcopy",
                    "-O",
                    "binary",
                    "--only-section=.notes",
                    str(workspace / "vmlinux"),
                    note_file.name,
                ],
                timeout=OBJCOPY_TIMEOUT_S,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "objcopy exceeded the build-id extraction timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": OBJCOPY_TIMEOUT_S},
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CategorizedError(
                "objcopy failed to extract vmlinux notes",
                category=ErrorCategory.BUILD_FAILURE,
            ) from exc
        except OSError as exc:
            raise launch_failure(
                "objcopy", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
            ) from exc
        notes = read_bytes_file(
            Path(note_file.name),
            category=ErrorCategory.BUILD_FAILURE,
            output="vmlinux notes",
        )
    return parse_gnu_build_id(notes)


def build_failure(message: str, run_id: UUID) -> CategorizedError:
    """A build failure with run-id details."""
    return CategorizedError(
        message, category=ErrorCategory.BUILD_FAILURE, details={"run_id": str(run_id)}
    )
