"""Unit tests for transport-backed orchestrator seams (Task 7 — ADR-0342).

Tests cover:
1. Call-order invariant: clone → defconfig → write fragment → merge_config.sh
   → olddefconfig → read .config → make.
2. Worker-side validation: _validate_final_config runs on the transport-read .config;
   a missing required symbol raises CONFIGURATION_ERROR.
3. Patch path: patch bytes are write_bytes-shipped and git apply runs via t.run;
   unchanged post-apply targets trigger the silent-skip CONFIGURATION_ERROR.
4. transport_run_step / transport_run_make / transport_run_olddefconfig build the
   expected argv with the correct -j flag.

No DB, no real subprocess, no real filesystem beyond tmp_path for workspace roots.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.build_host.orchestration import BuildHostOrchestrator
from kdive.providers.build_host.transport_seams import (
    transport_git_checkout,
    transport_read_config,
    transport_run_make,
    transport_run_olddefconfig,
    transport_run_step,
)
from kdive.providers.ports.build_transport import CommandResult
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("44444444-4444-4444-4444-444444444444")

# A .config satisfying both kdump + debuginfo requirements.
_GOOD_CONFIG = "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF5=y\n"
_FRAGMENT_BYTES = _GOOD_CONFIG.encode()

_VALID_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
    "patch_ref": None,
}

_GIT_REMOTE = "https://git.kernel.org/pub/scm/linux.git"
_GIT_REF = "v6.9"

_GOOD_PATCH = (
    "--- a/init/main.c\n+++ b/init/main.c\n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2-patched\n"
)


def _profile(extra: dict[str, Any] | None = None) -> ServerBuildProfile:
    data = {**_VALID_PROFILE, **(extra or {})}
    parsed = BuildProfile.parse(data)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


# ---------------------------------------------------------------------------
# FakeBuildTransport
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    """A single recorded transport call."""

    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass
class FakeBuildTransport:
    """Records every transport call and returns canned results.

    Args:
        config_text: Text returned by read_text when the path ends in ".config".
        file_bytes: Bytes returned by read_bytes (canned target bytes before apply).
        apply_changes_file: If True, post-apply read_bytes returns different bytes
            (simulating that git apply actually changed the target). If False,
            bytes are unchanged — triggering the silent-skip guard. NOTE: the
            before/after parity trick in read_bytes (``_read_count % 2``) assumes a
            SINGLE-TARGET patch — with two targets the before-reads interleave with
            the after-reads and the parity flips wrongly. The patch fixtures here are
            single-target; multi-target coverage would need per-path state.
        run_returncode: Default returncode for every run() call.
        run_results: Optional queue of CommandResults consumed in order, one per run()
            call; lets a test fail or set stderr on a SPECIFIC step (e.g. olddefconfig
            or the git-apply skip guard) instead of every run(). Once exhausted, run()
            falls back to ``run_returncode`` with empty stdout/stderr.
    """

    config_text: str = _GOOD_CONFIG
    file_bytes: bytes = b"original-content"
    apply_changes_file: bool = True
    run_returncode: int = 0
    run_results: list[CommandResult] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    _read_count: int = field(default=0, init=False)
    _run_count: int = field(default=0, init=False)

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(_Call(method=method, args=args, kwargs=kwargs))

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        self._record("run", argv, cwd=cwd, timeout_s=timeout_s)
        index = self._run_count
        self._run_count += 1
        if index < len(self.run_results):
            return self.run_results[index]
        return CommandResult(returncode=self.run_returncode, stdout="", stderr="")

    def read_text(self, path: str) -> str:
        self._record("read_text", path)
        return self.config_text

    def read_bytes(self, path: str) -> bytes:
        self._record("read_bytes", path)
        self._read_count += 1
        # First read (before apply) returns original bytes; second (after apply) returns
        # changed bytes when apply_changes_file is True, unchanged otherwise. This parity
        # trick assumes a single-target patch (see class docstring).
        if self.apply_changes_file and self._read_count % 2 == 0:
            return b"patched-content"
        return self.file_bytes

    def write_bytes(self, path: str, data: bytes) -> None:
        self._record("write_bytes", path, data=data)

    def clone(self, remote: str, ref: str, dest: str) -> None:
        self._record("clone", remote, ref, dest)

    def upload_file(self, path: str, presigned: Any) -> str:  # pragma: no cover
        self._record("upload_file", path, presigned=presigned)
        return "fake-etag"

    def cleanup(self, path: str) -> None:  # pragma: no cover
        self._record("cleanup", path)

    def method_names(self) -> list[str]:
        """Return the ordered list of called method names."""
        return [c.method for c in self.calls]

    def run_argvs(self) -> list[list[str]]:
        """Return the argv list for every run() call."""
        return [c.args[0] for c in self.calls if c.method == "run"]


# ---------------------------------------------------------------------------
# Helper: build an orchestrator wired to FakeBuildTransport
# ---------------------------------------------------------------------------


def _orchestrator(
    transport: FakeBuildTransport,
    tmp_path: Path,
    *,
    git_remote: str = _GIT_REMOTE,
    git_ref: str = _GIT_REF,
) -> BuildHostOrchestrator:
    """Wire a BuildHostOrchestrator with all transport-backed seams."""
    registry = SecretRegistry()
    return BuildHostOrchestrator.create(
        workspace_root=tmp_path / "workspace",
        catalog_fetch=lambda _name: _FRAGMENT_BYTES,
        checkout=transport_git_checkout(transport, git_remote, git_ref, registry),
        run_olddefconfig=transport_run_olddefconfig(transport),
        read_config=transport_read_config(transport),
        run_make=transport_run_make(transport),
    )


# ---------------------------------------------------------------------------
# 1. Call-order invariant
# ---------------------------------------------------------------------------


def test_build_workspace_call_order(tmp_path: Path) -> None:
    """build_workspace drives: clone → defconfig → write fragment → merge_config.sh
    → olddefconfig → read .config → make, in that exact order."""
    transport = FakeBuildTransport()
    orch = _orchestrator(transport, tmp_path)

    orch.build_workspace(_RUN, _profile())

    methods = transport.method_names()
    # clone must come first
    assert methods[0] == "clone"
    # write_bytes ships the fragment (before merge_config.sh runs)
    assert "write_bytes" in methods
    fragment_idx = next(i for i, m in enumerate(methods) if m == "write_bytes")
    # defconfig (the first run()) must precede the fragment write — a write-then-defconfig
    # reorder would clobber the fragment, so this catches it.
    first_run_idx = next(i for i, m in enumerate(methods) if m == "run")
    assert first_run_idx < fragment_idx
    # merge_config.sh is a run() — find the run() after the write_bytes
    run_after_fragment = [i for i, m in enumerate(methods) if m == "run" and i > fragment_idx]
    assert run_after_fragment, "No run() after fragment write"
    # olddefconfig run() comes after the checkout sequence (after clone)
    # read_text fetches the .config
    assert "read_text" in methods
    # make is the last run()
    last_run_idx = max(i for i, m in enumerate(methods) if m == "run")
    read_text_idx = next(i for i, m in enumerate(methods) if m == "read_text")
    assert read_text_idx < last_run_idx


def test_build_workspace_clone_receives_correct_remote_and_ref(tmp_path: Path) -> None:
    """transport.clone is called with the configured remote and ref."""
    transport = FakeBuildTransport()
    orch = _orchestrator(transport, tmp_path, git_remote=_GIT_REMOTE, git_ref=_GIT_REF)

    orch.build_workspace(_RUN, _profile())

    clone_call = next(c for c in transport.calls if c.method == "clone")
    assert clone_call.args[0] == _GIT_REMOTE
    assert clone_call.args[1] == _GIT_REF


def test_build_workspace_fragment_bytes_are_written(tmp_path: Path) -> None:
    """The catalog fragment bytes are shipped via write_bytes to the workspace."""
    transport = FakeBuildTransport()
    orch = _orchestrator(transport, tmp_path)

    orch.build_workspace(_RUN, _profile())

    write_calls = [c for c in transport.calls if c.method == "write_bytes"]
    assert write_calls, "No write_bytes call recorded"
    # The fragment bytes from catalog_fetch must be in one of the write_bytes calls.
    written_data = {c.kwargs.get("data") or c.args[1] for c in write_calls}
    assert _FRAGMENT_BYTES in written_data


def test_build_workspace_make_is_last_run(tmp_path: Path) -> None:
    """make (the full build) is the last run() call."""
    transport = FakeBuildTransport()
    orch = _orchestrator(transport, tmp_path)

    orch.build_workspace(_RUN, _profile())

    argvs = transport.run_argvs()
    assert argvs, "No run() calls recorded"
    last_argv = argvs[-1]
    assert last_argv[0] == "make"
    assert any(tok.startswith("-j") for tok in last_argv), f"No -j flag in make argv: {last_argv}"


def test_build_workspace_olddefconfig_before_read_config(tmp_path: Path) -> None:
    """olddefconfig run() precedes the read_text(.config) call."""
    transport = FakeBuildTransport()
    orch = _orchestrator(transport, tmp_path)

    orch.build_workspace(_RUN, _profile())

    methods = transport.method_names()
    # olddefconfig is a run() — the one whose argv contains "olddefconfig"
    runs = [(i, c) for i, c in enumerate(transport.calls) if c.method == "run"]
    olddefconfig_idx = next(i for i, c in runs if any("olddefconfig" in tok for tok in c.args[0]))
    read_text_idx = next(i for i, m in enumerate(methods) if m == "read_text")
    assert olddefconfig_idx < read_text_idx


# ---------------------------------------------------------------------------
# 2. Worker-side validation: _validate_final_config uses the transport-read .config
# ---------------------------------------------------------------------------


def test_build_workspace_missing_kdump_symbol_raises_configuration_error(tmp_path: Path) -> None:
    """A .config missing CONFIG_CRASH_DUMP raises CONFIGURATION_ERROR before make."""
    bad_config = "CONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF5=y\n"
    transport = FakeBuildTransport(config_text=bad_config)
    orch = _orchestrator(transport, tmp_path)

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, _profile())

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR
    # make (the full parallel build) must NOT have run
    full_make_ran = any(
        any(tok.startswith("-j") for tok in argv)
        for argv in transport.run_argvs()
        if argv and argv[0] == "make"
    )
    assert not full_make_ran


def test_build_workspace_missing_debuginfo_symbol_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """A .config missing all debuginfo options raises CONFIGURATION_ERROR before make."""
    bad_config = "CONFIG_CRASH_DUMP=y\n"
    transport = FakeBuildTransport(config_text=bad_config)
    orch = _orchestrator(transport, tmp_path)

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, _profile())

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_build_workspace_dropped_fragment_symbol_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """A fragment symbol absent from the read-back .config raises CONFIGURATION_ERROR."""
    # Fragment requests CONFIG_PROC_VMCORE; transport returns a .config that dropped it.
    fragment = b"CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO_DWARF5=y\nCONFIG_PROC_VMCORE=y\n"
    final_config = (
        "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO_DWARF5=y\n# CONFIG_PROC_VMCORE is not set\n"
    )
    transport = FakeBuildTransport(config_text=final_config)
    orch = BuildHostOrchestrator.create(
        workspace_root=tmp_path / "workspace",
        catalog_fetch=lambda _name: fragment,
        checkout=transport_git_checkout(transport, _GIT_REMOTE, _GIT_REF, SecretRegistry()),
        run_olddefconfig=transport_run_olddefconfig(transport),
        read_config=transport_read_config(transport),
        run_make=transport_run_make(transport),
    )

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, _profile())

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR
    dropped = exc_info.value.details.get("dropped")
    assert isinstance(dropped, list)
    assert "CONFIG_PROC_VMCORE" in dropped


# ---------------------------------------------------------------------------
# 3. Patch path: write_bytes + git apply; silent-skip raises CONFIGURATION_ERROR
# ---------------------------------------------------------------------------


def test_build_workspace_with_patch_ref_ships_bytes_and_runs_git_apply(
    tmp_path: Path,
) -> None:
    """With patch_ref set: patch bytes are write_bytes'd and git apply runs via t.run."""
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_GOOD_PATCH)

    transport = FakeBuildTransport(apply_changes_file=True)
    orch = _orchestrator(transport, tmp_path)
    profile = _profile({"patch_ref": str(patch_file)})

    orch.build_workspace(_RUN, profile)

    # At least one write_bytes should carry the patch bytes
    write_calls = [c for c in transport.calls if c.method == "write_bytes"]
    patch_bytes = patch_file.read_bytes()
    patch_written = any(
        (c.kwargs.get("data") or (c.args[1] if len(c.args) > 1 else None)) == patch_bytes
        for c in write_calls
    )
    assert patch_written, "Patch bytes were not shipped via write_bytes"

    # At least one run() should contain "git apply"
    apply_run = [argv for argv in transport.run_argvs() if "git" in argv and "apply" in argv]
    assert apply_run, "No 'git apply' run() call found"


def test_build_workspace_patch_unchanged_targets_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """Silent-skip guard: unchanged post-apply targets raise CONFIGURATION_ERROR."""
    patch_file = tmp_path / "noop.patch"
    patch_file.write_text(_GOOD_PATCH)

    # apply_changes_file=False → read_bytes always returns the same bytes → unchanged
    transport = FakeBuildTransport(apply_changes_file=False)
    orch = _orchestrator(transport, tmp_path)
    profile = _profile({"patch_ref": str(patch_file)})

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, profile)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_build_workspace_patch_skipped_patch_stderr_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """Silent-skip stderr guard: git apply exits 0 but prints 'Skipped patch ...'.

    The post-apply bytes are CHANGED (apply_changes_file=True), so only the stderr guard
    — not the content backstop — can catch this. Deleting the stderr check would let this
    build of an unpatched-where-skipped kernel through.
    """
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_GOOD_PATCH)

    # run() order in the checkout: [0]=defconfig, [1]=merge_config.sh, [2]=git apply.
    # Make git apply exit 0 while reporting a skipped file on stderr.
    transport = FakeBuildTransport(
        apply_changes_file=True,
        run_results=[
            CommandResult(returncode=0, stdout="", stderr=""),  # defconfig
            CommandResult(returncode=0, stdout="", stderr=""),  # merge_config.sh
            CommandResult(returncode=0, stdout="", stderr="Skipped patch 'init/main.c'.\n"),
        ],
    )
    orch = _orchestrator(transport, tmp_path)
    profile = _profile({"patch_ref": str(patch_file)})

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, profile)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_build_workspace_patch_nonzero_git_apply_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """git apply exiting non-zero raises CONFIGURATION_ERROR (apply-result guard)."""
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(_GOOD_PATCH)

    transport = FakeBuildTransport(
        run_results=[
            CommandResult(returncode=0, stdout="", stderr=""),  # defconfig
            CommandResult(returncode=0, stdout="", stderr=""),  # merge_config.sh
            CommandResult(returncode=1, stdout="", stderr="error: patch failed"),  # git apply
        ],
    )
    orch = _orchestrator(transport, tmp_path)
    profile = _profile({"patch_ref": str(patch_file)})

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, profile)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


# ---------------------------------------------------------------------------
# 4. transport_run_step / transport_run_make / transport_run_olddefconfig argv
# ---------------------------------------------------------------------------


def test_transport_run_step_builds_make_argv(tmp_path: Path) -> None:
    """transport_run_step builds ['make', '-C', ws, *args] and calls t.run with them."""
    transport = FakeBuildTransport()
    ws = tmp_path / "ws"
    step = transport_run_step(transport, ["olddefconfig"])

    step(ws)

    assert transport.run_argvs() == [["make", "-C", str(ws), "olddefconfig"]]


def test_transport_run_step_returns_transport_exit_code(tmp_path: Path) -> None:
    """transport_run_step returns the CommandResult.returncode from t.run."""
    transport = FakeBuildTransport(run_returncode=42)
    ws = tmp_path / "ws"
    step = transport_run_step(transport, ["targets"])

    assert step(ws) == 42


def test_transport_run_make_argv_includes_j_flag(tmp_path: Path) -> None:
    """transport_run_make builds ['make', '-C', ws, '-j<N>'] with N >= 1."""
    transport = FakeBuildTransport()
    ws = tmp_path / "ws"
    step = transport_run_make(transport)

    step(ws)

    argvs = transport.run_argvs()
    assert len(argvs) == 1
    argv = argvs[0]
    assert argv[:3] == ["make", "-C", str(ws)]
    j_tokens = [tok for tok in argv if tok.startswith("-j")]
    assert j_tokens, f"No -j flag in argv: {argv}"
    assert int(j_tokens[0][2:]) >= 1


def test_transport_run_make_j_matches_cpu_count(tmp_path: Path) -> None:
    """transport_run_make uses os.cpu_count() or 1 — the same logic as real_run_make."""
    transport = FakeBuildTransport()
    ws = tmp_path / "ws"
    expected_j = os.cpu_count() or 1
    step = transport_run_make(transport)

    step(ws)

    argv = transport.run_argvs()[0]
    assert f"-j{expected_j}" in argv


def test_transport_run_olddefconfig_argv(tmp_path: Path) -> None:
    """transport_run_olddefconfig builds ['make', '-C', ws, 'olddefconfig']."""
    transport = FakeBuildTransport()
    ws = tmp_path / "ws"
    step = transport_run_olddefconfig(transport)

    step(ws)

    assert transport.run_argvs() == [["make", "-C", str(ws), "olddefconfig"]]


def test_transport_read_config_reads_dot_config(tmp_path: Path) -> None:
    """transport_read_config reads <workspace>/.config via t.read_text."""
    ws = tmp_path / "ws"
    transport = FakeBuildTransport(config_text="CONFIG_CRASH_DUMP=y\n")
    reader = transport_read_config(transport)

    result = reader(ws)

    assert result == "CONFIG_CRASH_DUMP=y\n"
    read_calls = [c for c in transport.calls if c.method == "read_text"]
    assert any(str(ws / ".config") in c.args[0] for c in read_calls)


# ---------------------------------------------------------------------------
# 5. Edge: non-zero make steps raise build_failure
# ---------------------------------------------------------------------------


def test_build_workspace_nonzero_defconfig_raises_build_failure(tmp_path: Path) -> None:
    """A non-zero defconfig run() (the first run() in checkout) raises BUILD_FAILURE."""
    transport = FakeBuildTransport(run_returncode=1)
    orch = _orchestrator(transport, tmp_path)

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, _profile())

    assert exc_info.value.category is ErrorCategory.BUILD_FAILURE
    # defconfig is the first run(): it fails before any further run() (merge/olddefconfig).
    assert len(transport.run_argvs()) == 1
    assert "defconfig" in transport.run_argvs()[0]


def test_build_workspace_nonzero_olddefconfig_raises_build_failure(tmp_path: Path) -> None:
    """defconfig + merge_config succeed; only olddefconfig fails → BUILD_FAILURE.

    run() order: [0]=defconfig, [1]=merge_config.sh, [2]=olddefconfig. Failing only the
    olddefconfig step proves the orchestrator's olddefconfig gate (not the checkout's
    defconfig) maps a non-zero exit to BUILD_FAILURE.
    """
    transport = FakeBuildTransport(
        run_results=[
            CommandResult(returncode=0, stdout="", stderr=""),  # defconfig
            CommandResult(returncode=0, stdout="", stderr=""),  # merge_config.sh
            CommandResult(returncode=2, stdout="", stderr=""),  # olddefconfig
        ],
    )
    orch = _orchestrator(transport, tmp_path)

    with pytest.raises(CategorizedError) as exc_info:
        orch.build_workspace(_RUN, _profile())

    assert exc_info.value.category is ErrorCategory.BUILD_FAILURE
    # olddefconfig ran (3 run() calls); the .config was never read and make never ran.
    argvs = transport.run_argvs()
    assert any("olddefconfig" in argv for argv in argvs)
    assert "read_text" not in transport.method_names()
    assert not any(any(tok.startswith("-j") for tok in argv) for argv in argvs)
