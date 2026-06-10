"""Gated smoke test for the container image (ADR-0088 Phase 2).

Opt-in: set ``KDIVE_IMAGE`` to a built image tag and have ``docker`` on PATH. The
CI ``image-build`` job builds ``kdive:ci`` and runs this; locally,
``KDIVE_IMAGE=kdive:dev uv run pytest tests/image/test_image_smoke.py -q``.

It asserts the image's behaviour at the boundary CI can reach without backends:
the entrypoint lists every subcommand, each app command dispatches past argparse
into ADR-0087 config validation (a configuration_error, not an argparse error),
and the worker toolchain resolves on PATH for the non-root user.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KDIVE_IMAGE") is None or shutil.which("docker") is None,
    reason="set KDIVE_IMAGE and have docker to run the image smoke test",
)

_COMMANDS = ("server", "worker", "reconciler", "migrate")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _image() -> str:
    img = os.environ.get("KDIVE_IMAGE")
    assert img is not None  # narrowed for the type checker; skipif guards the None case
    return img


def test_entrypoint_lists_subcommands() -> None:
    res = _run(_image(), "--help")
    assert res.returncode == 0, res.stderr
    for cmd in _COMMANDS:
        assert cmd in res.stdout


def test_each_command_dispatches_past_argparse() -> None:
    # With no KDIVE_* config the command must reach ADR-0087 validation and fail
    # with a configuration error — proving it dispatched, not that argparse rejected
    # an unknown subcommand (which would exit 2 with a "invalid choice" usage error).
    img = _image()
    for cmd in _COMMANDS:
        res = _run(img, cmd)
        assert res.returncode != 0, f"{cmd} unexpectedly succeeded without config"
        combined = res.stdout + res.stderr
        assert "configuration" in combined.lower(), f"{cmd}: not a config failure: {combined}"
        assert "invalid choice" not in combined, f"{cmd}: argparse rejected the subcommand"


def test_worker_toolchain_on_path() -> None:
    img = _image()
    for tool in ("drgn", "gdb", "virsh"):
        res = _run("--entrypoint", tool, img, "--version")
        assert res.returncode == 0, f"{tool} missing: {res.stderr}"
