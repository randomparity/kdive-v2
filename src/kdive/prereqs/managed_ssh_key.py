"""Provision and resolve kdive's dedicated managed SSH keypair (ADR 0052).

kdive owns a durable ed25519 keypair (never ``~/.ssh``): the public half is baked into
the rootfs image at build, the private half is the future connect-time ``ssh -i`` identity.
This module is the single source of truth for both the key *path* and its *generation*, so
the builder (which shells out to the CLI) and the Python connect path never disagree.

It is intentionally **stdlib-only** and avoids 3.11+ runtime-only constructs: the builder
invokes it through the host's ``python3``, which may predate the project's venv.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

KEY_DIR_ENV = "KDIVE_SSH_KEY_DIR"
PRIVATE_KEY_NAME = "id_kdive_ed25519"  # pragma: allowlist secret  (filename, not a secret)
PUBLIC_KEY_NAME = "id_kdive_ed25519.pub"  # pragma: allowlist secret  (filename, not a secret)
KEY_COMMENT = "kdive-managed"


class ManagedKeyError(RuntimeError):
    """Resolving, generating, or securing the managed keypair failed."""


def _has_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _home(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    return Path(home) if home else Path.home()


def managed_key_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the managed key directory (no I/O).

    ``KDIVE_SSH_KEY_DIR`` wins when set (and must be absolute); otherwise
    ``$XDG_DATA_HOME/kdive/ssh`` when ``XDG_DATA_HOME`` is set, non-empty, and absolute;
    otherwise ``~/.local/share/kdive/ssh``.

    Raises:
        ManagedKeyError: ``KDIVE_SSH_KEY_DIR`` is empty/relative, or the resolved path
            contains a control character (it is pasted into a ``virt-builder`` argument).
    """
    environ = os.environ if env is None else env
    override = environ.get(KEY_DIR_ENV)
    if override is not None:
        candidate = Path(override).expanduser()
        if not override or not candidate.is_absolute():
            raise ManagedKeyError(
                f"{KEY_DIR_ENV} must be a non-empty absolute path; got {override!r}"
            )
        key_dir = candidate
    else:
        xdg = environ.get("XDG_DATA_HOME")
        if xdg and Path(xdg).is_absolute():
            key_dir = Path(xdg) / "kdive" / "ssh"
        else:
            key_dir = _home(environ) / ".local" / "share" / "kdive" / "ssh"
    if _has_control_character(str(key_dir)):
        raise ManagedKeyError(f"managed key directory contains a control character: {key_dir!r}")
    return key_dir


def managed_private_key_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the managed private-key path (no I/O)."""
    return managed_key_dir(env) / PRIVATE_KEY_NAME


def managed_public_key_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the managed public-key path (no I/O)."""
    return managed_key_dir(env) / PUBLIC_KEY_NAME


def ensure_managed_keypair(env: Mapping[str, str] | None = None) -> Path:
    """Generate the managed keypair if absent; return the private-key path. Idempotent.

    Creates the ``0700`` directory, then under an ``flock`` re-checks both halves: generates
    the pair if the private key is absent, re-derives only the public half if it is missing,
    and otherwise re-asserts ``0600`` on the private key. Never overwrites an existing private
    key.

    Raises:
        ManagedKeyError: the directory cannot be made private, or ``ssh-keygen`` is missing
            or fails.
    """
    private_key = managed_private_key_path(env)
    public_key = managed_public_key_path(env)
    key_dir = private_key.parent
    _ensure_private_dir(key_dir)
    lock_fd = os.open(str(key_dir / ".keygen.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not private_key.exists():
            _generate_keypair(private_key, public_key)
        elif not public_key.exists():
            _rederive_public_key(private_key, public_key)
        else:
            _enforce_private_mode(private_key)
    finally:
        os.close(lock_fd)
    return private_key


def _ensure_private_dir(key_dir: Path) -> None:
    if key_dir.is_symlink():
        raise ManagedKeyError(f"managed key directory is a symlink; refusing: {key_dir}")
    try:
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = key_dir.stat()
    except OSError as exc:
        raise ManagedKeyError(f"cannot create managed key directory {key_dir}: {exc}") from exc
    if info.st_uid != os.getuid():
        raise ManagedKeyError(f"managed key directory not owned by the current user: {key_dir}")
    if info.st_mode & 0o077:
        raise ManagedKeyError(
            f"managed key directory is group/other-accessible; refusing: {key_dir} "
            f"(mode {stat.S_IMODE(info.st_mode):#o})"
        )


def _generate_keypair(private_key: Path, public_key: Path) -> None:
    _run_keygen(
        [
            _keygen_executable(),
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private_key),
            "-C",
            KEY_COMMENT,
            "-q",
        ]
    )
    _enforce_private_mode(private_key)
    # ssh-keygen creates the public half subject to the process umask (0644 only under a
    # permissive umask; 0600 under umask 077), so chmod it explicitly to match the
    # re-derive path and keep the mode deterministic regardless of the build host's umask.
    try:
        os.chmod(public_key, 0o644)
    except OSError as exc:
        raise ManagedKeyError(f"cannot set mode on managed public key {public_key}: {exc}") from exc


def _rederive_public_key(private_key: Path, public_key: Path) -> None:
    material = _run_keygen([_keygen_executable(), "-y", "-f", str(private_key)])
    try:
        fd = os.open(str(public_key), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as handle:
            handle.write(material)
        os.chmod(public_key, 0o644)
    except OSError as exc:
        raise ManagedKeyError(f"cannot write managed public key {public_key}: {exc}") from exc


def _keygen_executable() -> str:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        raise ManagedKeyError(
            "ssh-keygen not found on PATH; install the OpenSSH client, or set "
            "KDIVE_ROOTFS_AUTHORIZED_KEY to a public key file"
        )
    return executable


def _run_keygen(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            # Fixed ssh-keygen executable from _keygen_executable(); key paths are KDIVE-owned.
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )  # noqa: S603
    except FileNotFoundError as exc:
        raise ManagedKeyError(
            "ssh-keygen not found on PATH; install the OpenSSH client, or set "
            "KDIVE_ROOTFS_AUTHORIZED_KEY to a public key file"
        ) from exc
    except OSError as exc:
        raise ManagedKeyError(f"cannot run ssh-keygen: {exc}") from exc
    if completed.returncode != 0:
        raise ManagedKeyError(
            f"ssh-keygen failed (exit {completed.returncode}): {completed.stderr.strip()}"
        )
    return completed.stdout


def _enforce_private_mode(private_key: Path) -> None:
    try:
        os.chmod(private_key, 0o600)
    except OSError as exc:
        raise ManagedKeyError(f"cannot secure managed private key {private_key}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Ensure the managed keypair and print a key path.

    ``--ensure-public-key`` prints the public-key path; no flag prints the private-key path.
    The printed line is the module's own computed path (never ``ssh-keygen`` output). Errors
    print to stderr and exit non-zero.
    """
    args = sys.argv[1:] if argv is None else argv
    want_public = args == ["--ensure-public-key"]
    if args and not want_public:
        print(
            "Usage: python -m kdive.prereqs.managed_ssh_key [--ensure-public-key]", file=sys.stderr
        )
        return 2
    try:
        private_key = ensure_managed_keypair()
    except ManagedKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(managed_public_key_path() if want_public else private_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
