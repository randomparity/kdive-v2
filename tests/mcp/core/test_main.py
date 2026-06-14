"""CLI argument parsing for `python -m kdive`."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.__main__ import build_parser
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.images.planes.base import RootfsBuildOutput
from kdive.images.rootfs_command import run_build_rootfs
from kdive.providers.runtime import ProviderRuntime


def _resolver_with_plane(plane: object) -> object:
    """A fake resolver whose resolve() returns a ProviderRuntime carrying only ``plane``."""

    class _FakeResolver:
        def resolve(self, kind: ResourceKind) -> ProviderRuntime:
            assert kind is ResourceKind.LOCAL_LIBVIRT
            unused = cast(Any, object())
            return ProviderRuntime(
                profile_policy=unused,
                provisioner=unused,
                builder=unused,
                installer=unused,
                booter=unused,
                connector=unused,
                controller=unused,
                retriever=unused,
                crash_postmortem=unused,
                vmcore_introspector=unused,
                live_introspector=unused,
                rootfs_build_plane=cast(Any, plane),
            )

    return _FakeResolver()


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
        def build(self, spec: object) -> RootfsBuildOutput:
            seen_specs.append(spec)
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FakePlane()),
    )

    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(
        ["build-rootfs", "--dest", str(dest), "--releasever", "42", "--package", "drgn"]
    )
    run_build_rootfs(args)

    assert dest.read_bytes() == b"image-bytes"
    assert not produced.exists(), "the plane output is moved, not copied"
    assert seen_specs and seen_specs[0].releasever == "42"
    assert seen_specs[0].packages == ("drgn",)


def test_run_build_rootfs_prints_eval_safe_export_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`build-rootfs` prints exactly one eval-safe export line to stdout on success."""
    produced = tmp_path / "plane-workspace" / "fedora-kdive-ready-43.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FakePlane()),
    )
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    run_build_rootfs(args)

    out = capsys.readouterr().out
    assert out == f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest.resolve()))}\n", (
        "stdout is exactly the eval-safe wiring line and nothing else"
    )
    assert "sha256:abc" not in out, "the digest summary stays on stderr, never on stdout"


def test_run_build_rootfs_export_line_round_trips_a_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --dest with a space is a single shlex-quoted token that round-trips to the path."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FakePlane()),
    )
    dest = tmp_path / "with space" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    run_build_rootfs(args)

    out = capsys.readouterr().out.strip()
    assert out.startswith("export KDIVE_GUEST_IMAGE=")
    value = out[len("export KDIVE_GUEST_IMAGE=") :]
    assert shlex.split(value) == [str(dest.resolve())], "one token, round-trips to the path"


def test_run_build_rootfs_writes_nothing_to_stdout_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing build raises and prints no export line, so eval exports nothing."""

    class _FailingPlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            raise CategorizedError("build blew up", category=ErrorCategory.PROVISIONING_FAILURE)

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FailingPlane()),
    )
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    with pytest.raises(CategorizedError):
        run_build_rootfs(args)
    assert capsys.readouterr().out == "", "no export line is printed when the build fails"
