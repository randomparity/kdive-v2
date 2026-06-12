"""Tests for shared rootfs image build helpers."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes import _build_common
from kdive.images.planes._build_common import (
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)


def test_run_guestfs_tool_invokes_fixed_argv_with_timeout_and_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_build_common.subprocess, "run", _run)

    run_guestfs_tool(
        ["virt-builder", "fedora-43"],
        stage="build",
        timeout_s=15,
        missing_message="virt-builder is not installed",
        input_text="commands\n",
    )

    assert calls == [
        {
            "argv": ["virt-builder", "fedora-43"],
            "input": "commands\n",
            "capture_output": True,
            "text": True,
            "timeout": 15,
            "check": False,
        }
    ]


def test_run_guestfs_tool_maps_missing_tool_to_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(argv: list[str], **kwargs: object) -> NoReturn:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(_build_common.subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-builder"],
            stage="build",
            timeout_s=5,
            missing_message="virt-builder is not installed",
        )

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"stage": "build", "tool": "virt-builder"}


def test_run_guestfs_tool_maps_timeout_to_provisioning_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(argv: list[str], **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(argv, timeout=7)

    monkeypatch.setattr(_build_common.subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-builder"],
            stage="customize",
            timeout_s=7,
            missing_message="virt-builder is not installed",
        )

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert caught.value.details == {
        "stage": "customize",
        "tool": "virt-builder",
        "timeout_s": 7,
    }


def test_run_guestfs_tool_maps_launch_oserror_to_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _oserror(argv: list[str], **kwargs: object) -> NoReturn:
        raise PermissionError(argv[0])

    monkeypatch.setattr(_build_common.subprocess, "run", _oserror)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["guestfish"],
            stage="normalize",
            timeout_s=3,
            missing_message="guestfish is not installed",
        )

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {
        "stage": "normalize",
        "tool": "guestfish",
        "error": "PermissionError",
    }


def test_run_guestfs_tool_maps_nonzero_exit_with_truncated_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "x" * 2500 + "tail"

    def _failed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode=2, stdout="", stderr=stderr)

    monkeypatch.setattr(_build_common.subprocess, "run", _failed)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-make-fs"],
            stage="repack",
            timeout_s=60,
            missing_message="virt-make-fs is not installed",
            failure_message="repack failed",
        )

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(caught.value) == "repack failed"
    assert caught.value.details == {
        "stage": "repack",
        "tool": "virt-make-fs",
        "stderr": stderr[-2000:],
    }


@pytest.mark.parametrize("name", ["fedora", "Fedora_43", "kdive.ready-43", "image.1"])
def test_validate_image_name_accepts_filename_safe_names(name: str) -> None:
    validate_image_name(name)


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden", "-leading", "with space"])
def test_validate_image_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_image_name(name)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {"name": name}


def test_digest_file_returns_sha256_uri_for_file_content(tmp_path: Path) -> None:
    data = b"rootfs-bytes"
    path = tmp_path / "image.qcow2"
    path.write_bytes(data)

    assert digest_file(path) == f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_publish_qcow2_replaces_destination_with_scratch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch.qcow2"
    scratch.write_bytes(b"new-image")
    stale = workspace / "fedora.qcow2"
    stale.write_bytes(b"old-image")

    published = publish_qcow2(workspace, image_name="fedora", scratch=scratch)

    assert published == stale
    assert published.read_bytes() == b"new-image"
    assert not scratch.exists()


def test_build_workspace_creates_parent_and_cleans_temporary_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with build_workspace(workspace, prefix="rootfs-build-") as work_dir:
        assert workspace.is_dir()
        assert work_dir.parent == workspace
        assert work_dir.name.startswith("rootfs-build-")
        work_dir.joinpath("artifact").write_text("payload")
        created = work_dir

    assert workspace.is_dir()
    assert not created.exists()
