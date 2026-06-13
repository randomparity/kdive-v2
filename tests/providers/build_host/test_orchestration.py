"""Workspace lifecycle on the shared BuildHostOrchestrator (ADR-0102).

The orchestrator owns the per-run workspace: it derives the path, and — new in #358 —
removes it after a terminal build via an injected best-effort ``cleanup`` seam (default
``shutil.rmtree``; ``over_transport`` injects ``BuildTransport.cleanup``). These tests drive
the path-derivation and cleanup seam directly; the create/checkout/make orchestration is
covered through the provider build tests.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from kdive.providers.build_host.orchestration import BuildHostOrchestrator, WorkspaceCleanup

_RUN = UUID("44444444-4444-4444-4444-444444444444")


def _orchestrator(
    workspace_root: Path, *, cleanup: WorkspaceCleanup | None = None
) -> BuildHostOrchestrator:
    """An orchestrator with inert build seams; only the workspace lifecycle is exercised."""
    return BuildHostOrchestrator.create(
        workspace_root=workspace_root,
        catalog_fetch=lambda _name: b"",
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: 0,
        read_config=lambda _w: "",
        run_make=lambda _w: 0,
        cleanup=cleanup,
    )


def test_workspace_path_is_root_joined_with_run_id(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    assert orch.workspace_path(_RUN) == tmp_path / "ws" / str(_RUN)


def test_cleanup_workspace_invokes_injected_seam_with_path(tmp_path: Path) -> None:
    seen: list[Path] = []
    orch = _orchestrator(tmp_path / "ws", cleanup=seen.append)

    workspace = orch.workspace_path(_RUN)
    orch.cleanup_workspace(workspace)

    assert seen == [workspace]


def test_default_cleanup_removes_a_real_directory(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    workspace = orch.workspace_path(_RUN)
    workspace.mkdir(parents=True)
    (workspace / "vmlinux").write_bytes(b"x")

    orch.cleanup_workspace(workspace)

    assert not workspace.exists()


def test_default_cleanup_on_missing_path_does_not_raise(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path / "ws")
    orch.cleanup_workspace(orch.workspace_path(_RUN))  # never created — must be a no-op
