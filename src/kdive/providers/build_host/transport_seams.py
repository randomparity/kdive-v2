"""Transport-backed seam factories for BuildHostOrchestrator (Task 7 — ADR-0342).

These factories wrap a :class:`~kdive.providers.ports.build_transport.BuildTransport`
and return the ``RunStep``, ``ReadConfig``, and ``Checkout`` callables that
:class:`~kdive.providers.build_host.orchestration.BuildHostOrchestrator` expects.

The local warm-tree checkout (``make_checkout`` / ``real_checkout``) is unchanged.
This module provides the *git-provenance* checkout for the SSH path and the
transport-generic step helpers.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_validation import parse_gnu_build_id, patch_target_paths
from kdive.providers.build_host.config import resolve_local_ref
from kdive.providers.build_host.execution import (
    MAKE_TIMEOUT_S,
    OBJCOPY_TIMEOUT_S,
    ReadBuildId,
    ReadConfig,
    RunModulesInstall,
    RunStep,
    build_failure,
)
from kdive.providers.build_host.workspace import GIT_APPLY_TIMEOUT_S, Checkout, redacted_tail
from kdive.providers.ports.build_transport import BuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry

# ---------------------------------------------------------------------------
# Step factories
# ---------------------------------------------------------------------------


def transport_run_step(
    t: BuildTransport,
    args: list[str],
    timeout_s: int = MAKE_TIMEOUT_S,
) -> RunStep:
    """Return a ``RunStep`` that runs ``make -C <ws> <args...>`` over the transport.

    Args:
        t: The build transport to dispatch the command through.
        args: Extra arguments appended after ``-C <workspace>``.
        timeout_s: Hard deadline passed to :meth:`BuildTransport.run`.

    Returns:
        A callable ``(workspace: Path) -> int`` matching the ``RunStep`` type alias.
    """

    def _step(ws: Path) -> int:
        return t.run(["make", "-C", str(ws), *args], cwd=str(ws), timeout_s=timeout_s).returncode

    return _step


def transport_run_make(t: BuildTransport) -> RunStep:
    """Return a ``RunStep`` for the parallel kernel build, using ``os.cpu_count()`` jobs.

    Mirrors ``real_run_make``'s ``-j{os.cpu_count() or 1}`` parallelism exactly.

    Args:
        t: The build transport to dispatch ``make`` through.

    Returns:
        A callable ``(workspace: Path) -> int`` matching the ``RunStep`` type alias.
    """
    return transport_run_step(t, [f"-j{os.cpu_count() or 1}"])


def transport_run_olddefconfig(t: BuildTransport) -> RunStep:
    """Return a ``RunStep`` for ``make olddefconfig`` over the transport.

    Args:
        t: The build transport to dispatch ``make olddefconfig`` through.

    Returns:
        A callable ``(workspace: Path) -> int`` matching the ``RunStep`` type alias.
    """
    return transport_run_step(t, ["olddefconfig"])


def transport_read_config(t: BuildTransport) -> ReadConfig:
    """Return a ``ReadConfig`` that reads ``<workspace>/.config`` via the transport.

    Args:
        t: The build transport to read from.

    Returns:
        A callable ``(workspace: Path) -> str`` matching the ``ReadConfig`` type alias.
    """

    def _read(ws: Path) -> str:
        return t.read_text(str(ws / ".config"))

    return _read


# ---------------------------------------------------------------------------
# Post-make step factories
# ---------------------------------------------------------------------------


def transport_run_modules_install(t: BuildTransport) -> RunModulesInstall:
    """Return a ``RunModulesInstall`` running ``make modules_install`` over the transport.

    Mirrors ``real_run_modules_install``'s argv exactly — ``make -C <ws>
    INSTALL_MOD_PATH=<mod_root> modules_install`` — staging the module tree at *mod_root* on
    the transport's host.

    Args:
        t: The build transport to dispatch ``make modules_install`` through.

    Returns:
        A callable ``(workspace: Path, mod_root: Path) -> int`` matching ``RunModulesInstall``.
    """

    def _step(ws: Path, mod_root: Path) -> int:
        argv = ["make", "-C", str(ws), f"INSTALL_MOD_PATH={mod_root}", "modules_install"]
        return t.run(argv, cwd=str(ws), timeout_s=MAKE_TIMEOUT_S).returncode

    return _step


def transport_read_build_id(t: BuildTransport) -> ReadBuildId:
    """Return a ``ReadBuildId`` extracting ``vmlinux``'s GNU build-id over the transport.

    Mirrors ``real_read_build_id``: ``objcopy`` writes the ``.notes`` section to a sibling
    file on the host, the (small) note blob is read back to the worker via ``read_bytes``, and
    :func:`parse_gnu_build_id` parses it on the worker. Only the note — never ``vmlinux`` —
    crosses the transport.

    Args:
        t: The build transport to run ``objcopy`` and read the note through.

    Returns:
        A callable ``(workspace: Path) -> str`` matching the ``ReadBuildId`` type alias.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if ``objcopy`` exits non-zero or the note carries
            no GNU build-id (from :func:`parse_gnu_build_id`).
    """

    def _read(ws: Path) -> str:
        note_path = str(ws / "vmlinux.note")
        argv = ["objcopy", "-O", "binary", "--only-section=.notes", str(ws / "vmlinux"), note_path]
        result = t.run(argv, cwd=str(ws), timeout_s=OBJCOPY_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "objcopy failed to extract vmlinux notes",
                category=ErrorCategory.BUILD_FAILURE,
                details={"stderr": result.stderr[-512:]},
            )
        return parse_gnu_build_id(t.read_bytes(note_path))

    return _read


# ---------------------------------------------------------------------------
# Git-provenance transport checkout
# ---------------------------------------------------------------------------


def transport_git_checkout(
    t: BuildTransport,
    git_remote: str,
    git_ref: str,
    secret_registry: SecretRegistry,
) -> Checkout:
    """Return a ``Checkout`` that clones via ``git`` and merges config over the transport.

    The returned callable mirrors ``real_checkout``'s logical sequence — clone, merge
    config, optional patch — but every filesystem and subprocess operation goes through
    *t* instead of the local environment.

    Args:
        t: Build transport (SSH or local) providing ``clone``, ``run``, ``read_bytes``,
            and ``write_bytes``.
        git_remote: Git remote URL to clone (validated by the transport's ``clone``).
        git_ref: Git ref (tag, branch, or commit SHA) to check out.
        secret_registry: Used to redact secrets in error details.

    Returns:
        A ``Checkout`` callable ``(run_id, profile, workspace, fragment_bytes) -> None``.
    """

    def _checkout(
        run_id: UUID,
        profile: ServerBuildProfile,
        workspace: Path,
        fragment_bytes: bytes,
    ) -> None:
        t.clone(git_remote, git_ref, str(workspace))
        _transport_merge_config(t, fragment_bytes, workspace, run_id)
        if profile.patch_ref is not None:
            _transport_apply_patch(t, profile.patch_ref, workspace, secret_registry)

    return _checkout


def _transport_merge_config(
    t: BuildTransport,
    fragment_bytes: bytes,
    workspace: Path,
    run_id: UUID,
) -> None:
    """Run defconfig, ship the fragment, and merge it — all over the transport.

    Mirrors ``merge_config``'s sequence and error mapping, using transport primitives
    instead of local subprocess and filesystem calls.

    Args:
        t: The build transport.
        fragment_bytes: Raw kernel config fragment to merge onto the base defconfig.
        workspace: Remote workspace path (on the transport's host).
        run_id: Run identifier for error details.
    """
    defconfig_result = t.run(
        ["make", "-C", str(workspace), "defconfig"],
        cwd=str(workspace),
        timeout_s=MAKE_TIMEOUT_S,
    )
    if defconfig_result.returncode != 0:
        raise build_failure("make defconfig exited non-zero", run_id)

    fragment_path = str(workspace / "kdump.config.fragment")
    t.write_bytes(fragment_path, fragment_bytes)

    merge_result = t.run(
        ["scripts/kconfig/merge_config.sh", "-m", ".config", fragment_path],
        cwd=str(workspace),
        timeout_s=MAKE_TIMEOUT_S,
    )
    if merge_result.returncode != 0:
        raise build_failure("merge_config.sh -m exited non-zero", run_id)


def _transport_apply_patch(
    t: BuildTransport,
    patch_ref: str,
    workspace: Path,
    secret_registry: SecretRegistry,
) -> None:
    """Resolve the patch locally, ship it, and apply it over the transport.

    Mirrors ``apply_patch``'s silent-skip guards: the ``patch_target_paths`` extraction
    and the before/after byte comparison are shared pure helpers; only the I/O goes
    through the transport.

    Args:
        t: The build transport.
        patch_ref: Local ref (absolute path or ``file://`` URL) to the patch file.
        workspace: Remote workspace path (on the transport's host).
        secret_registry: Used to redact secrets from error details.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the patch does not apply, is
            silently skipped by ``git apply``, or leaves the tree unchanged.
    """
    patch_path = resolve_local_ref(patch_ref, kind="patch_ref")
    patch_bytes = patch_path.read_bytes()
    patch_text = patch_bytes.decode(errors="replace")

    targets = patch_target_paths(patch_text, strip=1)

    remote_patch_path = str(workspace / patch_path.name)
    t.write_bytes(remote_patch_path, patch_bytes)

    before = {rel: t.read_bytes(str(workspace / rel)) for rel in targets}

    result = t.run(
        ["git", "apply", "-p1", "-v", "--", remote_patch_path],
        cwd=str(workspace),
        timeout_s=GIT_APPLY_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if any(line.startswith("Skipped patch ") for line in result.stderr.splitlines()):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but skipped one or "
            "more files as already applied (the build workspace has no .git, so git fell "
            "back to context matching)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )

    after = {rel: t.read_bytes(str(workspace / rel)) for rel in targets}
    if targets and all(after[rel] == before[rel] for rel in targets):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but left the kernel "
            "tree unchanged (the build workspace has no .git, so git fell back to context "
            "matching and treated the patch as already applied)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"targets": sorted(str(rel) for rel in targets)},
        )
