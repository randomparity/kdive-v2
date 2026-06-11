"""CLI argument parsing for `python -m kdive`."""

from __future__ import annotations

from pathlib import Path

import pytest

import kdive.__main__ as main_module
from kdive.__main__ import build_parser
from kdive.images.planes.base import RootfsBuildOutput


def test_server_subcommand_parses() -> None:
    args = build_parser().parse_args(["server"])
    assert args.command == "server"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_worker_subcommand_parses_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "worker"])
    assert args.command == "worker"
    assert args.log_level == "DEBUG"


def test_no_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_build_rootfs_subcommand_parses_with_defaults() -> None:
    args = build_parser().parse_args(["build-rootfs"])
    assert args.command == "build-rootfs"
    assert args.name == "fedora-kdive-ready-43"
    assert args.releasever == "43"
    assert args.packages is None  # falls back to the kdump/drgn default set in the handler


def test_build_rootfs_subcommand_collects_repeated_packages() -> None:
    args = build_parser().parse_args(
        ["build-rootfs", "--dest", "/tmp/out.qcow2", "--package", "drgn", "--package", "perf"]
    )
    assert args.dest == "/tmp/out.qcow2"
    assert args.packages == ["drgn", "perf"]


def test_run_build_rootfs_moves_plane_output_to_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build-rootfs` builds via the local plane and moves the qcow2 to ``--dest``."""
    produced = tmp_path / "plane-workspace" / "fedora-kdive-ready-43.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")
    seen_specs = []

    class _FakePlane:
        @classmethod
        def from_env(cls) -> _FakePlane:
            return cls()

        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.planes.local_libvirt.LocalLibvirtRootfsBuildPlane", _FakePlane
    )

    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-rootfs", "--dest", str(dest), "--releasever", "42", "--package", "drgn"]
    )
    main_module._run_build_rootfs(args)

    assert dest.read_bytes() == b"image-bytes"
    assert not produced.exists(), "the plane output is moved, not copied"
    assert seen_specs and seen_specs[0].releasever == "42"
    assert seen_specs[0].packages == ("drgn",)
