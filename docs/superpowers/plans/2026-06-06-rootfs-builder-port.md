# Bootable kdive-ready rootfs builder (port) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `scripts/live-vm/build-guest-image.sh` stub with the ported v1 two-stage libguestfs builder so an unprivileged run emits a whole-disk-ext4 qcow2 that boots to the `kdive-ready` console marker, and bring across the managed SSH-key helper the builder depends on.

**Architecture:** A new stdlib-only `src/kdive/prereqs/managed_ssh_key.py` owns kdive's durable ed25519 keypair (single source of truth for path + generation). The bash builder shells out to it (`python3 -m kdive.prereqs.managed_ssh_key --ensure-public-key`), bakes the public half + a `kdive-ready.service` oneshot into a Fedora scratch image via `virt-builder`, then repacks to a no-partition-table ext4 qcow2 via `virt-tar-out` + `virt-make-fs`. Two new safety changes over v1: a presence-only idempotency guard and an early output-dir preflight, both before any libguestfs tool runs.

**Tech Stack:** Bash (`set -euo pipefail`, shellcheck/shfmt), Python 3.13 (stdlib only; ruff `E,F,I,UP,B,SIM`, `ty`), libguestfs (`virt-builder`/`virt-tar-out`/`virt-make-fs`/`guestfish`), `qemu-img`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-06-rootfs-builder-port-design.md` · **ADR:** `docs/adr/0052-bootable-rootfs-image-builder.md`

**Source of truth for the port:** `~/src/kdive-v1/src/kdive/prereqs/managed_ssh_key.py`, `~/src/kdive-v1/tests/prereqs/test_managed_ssh_key.py`, `~/src/kdive-v1/scripts/build-rootfs.sh`.

---

## File structure

| Path | Action | Responsibility |
|------|--------|----------------|
| `src/kdive/prereqs/__init__.py` | Create | Package marker (one-line docstring). |
| `src/kdive/prereqs/managed_ssh_key.py` | Create | Managed ed25519 keypair: path resolution + idempotent generation + `python -m` CLI. |
| `tests/prereqs/__init__.py` | Create | Test package marker (mirrors `tests/scripts/__init__.py`). |
| `tests/prereqs/test_managed_ssh_key.py` | Create | Unit suite for the key helper (pure path resolution + `ssh-keygen`-gated generation + CLI). |
| `scripts/live-vm/build-guest-image.sh` | Overwrite | The two-stage rootfs builder (replaces the empty-qcow2 stub). |
| `tests/scripts/test_live_vm_fixtures.py` | Modify | Keep the existing parse/strict-mode/idempotency assertions valid for the ported builder; add a dedicated key-resolution-skip assertion. |
| `docs/runbooks/live-stack.md` | Modify | §3 description corrected to the rootfs build + host-side image-labeling note. |

---

## Task 1: Port the managed SSH-key helper

**Files:**
- Create: `src/kdive/prereqs/__init__.py`
- Create: `src/kdive/prereqs/managed_ssh_key.py`
- Create: `tests/prereqs/__init__.py`
- Test: `tests/prereqs/test_managed_ssh_key.py`

- [ ] **Step 1: Create the test package marker and port the failing test suite**

Create `tests/prereqs/__init__.py` containing exactly:

```python
```

(empty file — matches `tests/scripts/__init__.py`).

Copy the v1 test verbatim — its imports (`from kdive.prereqs.managed_ssh_key import ...`) already match the rewrite's package path:

```bash
cp ~/src/kdive-v1/tests/prereqs/test_managed_ssh_key.py tests/prereqs/test_managed_ssh_key.py
```

The file's full content (confirm it matches after copy):

```python
"""Unit tests for the kdive-managed SSH keypair helper (ADR 0052)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from kdive.prereqs.managed_ssh_key import (
    ManagedKeyError,
    ensure_managed_keypair,
    main,
    managed_key_dir,
    managed_private_key_path,
    managed_public_key_path,
)

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
needs_keygen = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen not installed")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- path resolution (pure, no I/O) ---------------------------------------------------


def test_dir_defaults_to_local_share_under_home() -> None:
    assert managed_key_dir(env={"HOME": "/home/u"}) == Path("/home/u/.local/share/kdive/ssh")


def test_dir_honors_absolute_xdg_data_home() -> None:
    env = {"HOME": "/home/u", "XDG_DATA_HOME": "/data/x"}
    assert managed_key_dir(env=env) == Path("/data/x/kdive/ssh")


def test_dir_ignores_empty_or_relative_xdg_data_home() -> None:
    base = {"HOME": "/home/u"}
    assert managed_key_dir(env={**base, "XDG_DATA_HOME": ""}) == Path("/home/u/.local/share/kdive/ssh")
    assert managed_key_dir(env={**base, "XDG_DATA_HOME": "rel/path"}) == Path("/home/u/.local/share/kdive/ssh")


def test_dir_override_wins() -> None:
    env = {"HOME": "/home/u", "XDG_DATA_HOME": "/data/x", "KDIVE_SSH_KEY_DIR": "/keys/kdive"}
    assert managed_key_dir(env=env) == Path("/keys/kdive")


def test_dir_override_must_be_absolute() -> None:
    with pytest.raises(ManagedKeyError, match="absolute"):
        managed_key_dir(env={"HOME": "/home/u", "KDIVE_SSH_KEY_DIR": "rel"})
    with pytest.raises(ManagedKeyError, match="absolute"):
        managed_key_dir(env={"HOME": "/home/u", "KDIVE_SSH_KEY_DIR": ""})


def test_dir_refuses_control_character() -> None:
    with pytest.raises(ManagedKeyError, match="control character"):
        managed_key_dir(env={"HOME": "/home/u", "KDIVE_SSH_KEY_DIR": "/keys/a\nb"})


def test_key_paths_are_under_dir() -> None:
    env = {"HOME": "/home/u"}
    assert managed_private_key_path(env=env) == Path("/home/u/.local/share/kdive/ssh/id_kdive_ed25519")
    assert managed_public_key_path(env=env) == Path("/home/u/.local/share/kdive/ssh/id_kdive_ed25519.pub")


def test_path_functions_do_no_io(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    managed_private_key_path(env=env)
    managed_public_key_path(env=env)
    assert not (tmp_path / ".local").exists()


# --- generation, idempotency, repair, modes -------------------------------------------


@needs_keygen
def test_generate_creates_keypair_with_modes(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    priv = ensure_managed_keypair(env=env)
    pub = managed_public_key_path(env=env)
    assert priv.exists() and pub.exists()
    assert pub.read_text().startswith("ssh-ed25519 ")
    assert _mode(priv) == 0o600
    assert _mode(pub) == 0o644
    assert _mode(priv.parent) == 0o700


@needs_keygen
def test_generate_is_idempotent(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    priv = ensure_managed_keypair(env=env)
    first = priv.read_bytes()
    again = ensure_managed_keypair(env=env)
    assert again == priv
    assert priv.read_bytes() == first  # never overwritten


@needs_keygen
def test_reasserts_private_mode(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    priv = ensure_managed_keypair(env=env)
    os.chmod(priv, 0o644)
    ensure_managed_keypair(env=env)
    assert _mode(priv) == 0o600


@needs_keygen
def test_rederives_missing_public_half(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    priv = ensure_managed_keypair(env=env)
    priv_bytes = priv.read_bytes()
    pub = managed_public_key_path(env=env)
    pub.unlink()
    ensure_managed_keypair(env=env)
    assert pub.exists()
    assert pub.read_text().startswith("ssh-ed25519 ")
    assert priv.read_bytes() == priv_bytes  # private key untouched
    assert _mode(pub) == 0o644


def test_refuses_group_or_other_accessible_dir(tmp_path: Path) -> None:
    key_dir = tmp_path / ".local" / "share" / "kdive" / "ssh"
    key_dir.mkdir(parents=True)
    os.chmod(key_dir, 0o755)
    with pytest.raises(ManagedKeyError, match="group/other-accessible"):
        ensure_managed_keypair(env={"HOME": str(tmp_path)})


def test_missing_ssh_keygen_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")  # no ssh-keygen resolvable
    with pytest.raises(ManagedKeyError, match="ssh-keygen"):
        ensure_managed_keypair(env={"HOME": str(tmp_path)})


def test_run_keygen_wraps_non_filenotfound_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("permission denied")

    monkeypatch.setattr("kdive.prereqs.managed_ssh_key.subprocess.run", _boom)
    with pytest.raises(ManagedKeyError, match="ssh-keygen"):
        ensure_managed_keypair(env={"HOME": str(tmp_path)})


# --- CLI -------------------------------------------------------------------------------


def _hermetic_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("KDIVE_SSH_KEY_DIR", raising=False)


@needs_keygen
def test_cli_ensure_public_prints_only_public_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hermetic_home(monkeypatch, tmp_path)
    rc = main(["--ensure-public-key"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{tmp_path}/.local/share/kdive/ssh/id_kdive_ed25519.pub\n"
    assert (tmp_path / ".local/share/kdive/ssh/id_kdive_ed25519.pub").exists()


@needs_keygen
def test_cli_default_prints_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hermetic_home(monkeypatch, tmp_path)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{tmp_path}/.local/share/kdive/ssh/id_kdive_ed25519\n"


def test_cli_rejects_unknown_args(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--bogus"])
    assert rc == 2
    assert "Usage" in capsys.readouterr().err


def test_cli_reports_generation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _hermetic_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", "")
    rc = main(["--ensure-public-key"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ssh-keygen" in err
    assert "KDIVE_ROOTFS_AUTHORIZED_KEY" in err
```

The only change from the v1 file is the docstring's ADR reference (`0057` → `0052`).

- [ ] **Step 2: Run the suite to verify it fails (no module yet)**

Run: `uv run python -m pytest tests/prereqs/test_managed_ssh_key.py -q`
Expected: collection error / `ModuleNotFoundError: No module named 'kdive.prereqs'`.

- [ ] **Step 3: Create the package marker**

Create `src/kdive/prereqs/__init__.py` containing exactly:

```python
"""Host prerequisite helpers (managed SSH keypair, …)."""
```

- [ ] **Step 4: Create the module (port verbatim, drop bandit `# nosec` pragmas)**

Copy then strip the bandit pragmas (this repo uses ruff, not bandit; keep the
`# pragma: allowlist secret` detect-secrets pragmas):

```bash
cp ~/src/kdive-v1/src/kdive/prereqs/managed_ssh_key.py src/kdive/prereqs/managed_ssh_key.py
```

Then edit `src/kdive/prereqs/managed_ssh_key.py` so it reads exactly as below (the
diff vs the v1 source is: ADR `0057`→`0052` in the docstring; remove the three
`# nosec Bxxx` comments on `import subprocess`, `subprocess.run(`, and the second
`subprocess.run(`):

```python
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
            raise ManagedKeyError(f"{KEY_DIR_ENV} must be a non-empty absolute path; got {override!r}")
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
    _run_keygen(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(private_key), "-C", KEY_COMMENT, "-q"])
    _enforce_private_mode(private_key)
    # ssh-keygen creates the public half subject to the process umask, so chmod it explicitly
    # to keep the mode deterministic regardless of the build host's umask.
    try:
        os.chmod(public_key, 0o644)
    except OSError as exc:
        raise ManagedKeyError(f"cannot set mode on managed public key {public_key}: {exc}") from exc


def _rederive_public_key(private_key: Path, public_key: Path) -> None:
    material = _run_keygen(["ssh-keygen", "-y", "-f", str(private_key)])
    try:
        fd = os.open(str(public_key), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as handle:
            handle.write(material)
        os.chmod(public_key, 0o644)
    except OSError as exc:
        raise ManagedKeyError(f"cannot write managed public key {public_key}: {exc}") from exc


def _run_keygen(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise ManagedKeyError(
            "ssh-keygen not found on PATH; install the OpenSSH client, or set "
            "KDIVE_ROOTFS_AUTHORIZED_KEY to a public key file"
        ) from exc
    except OSError as exc:
        raise ManagedKeyError(f"cannot run ssh-keygen: {exc}") from exc
    if completed.returncode != 0:
        raise ManagedKeyError(f"ssh-keygen failed (exit {completed.returncode}): {completed.stderr.strip()}")
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
        print("Usage: python -m kdive.prereqs.managed_ssh_key [--ensure-public-key]", file=sys.stderr)
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
```

- [ ] **Step 5: Run the suite to verify it passes**

Run: `uv run python -m pytest tests/prereqs/test_managed_ssh_key.py -q`
Expected: PASS (the four `@needs_keygen` and CLI generation tests run because `ssh-keygen` is installed on this host; otherwise they SKIP — both are green).

- [ ] **Step 6: Lint + type the new module**

Run: `uv run ruff check src/kdive/prereqs tests/prereqs && uv run ruff format --check src/kdive/prereqs tests/prereqs && uv run ty check`
Expected: all clean (no errors). If `ruff format --check` reports a diff, run `uv run ruff format src/kdive/prereqs tests/prereqs` and re-check.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/prereqs/__init__.py src/kdive/prereqs/managed_ssh_key.py \
        tests/prereqs/__init__.py tests/prereqs/test_managed_ssh_key.py
git commit -m "feat(prereqs): port the kdive-managed SSH keypair helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Replace the builder stub with the ported two-stage builder

**Files:**
- Modify: `tests/scripts/test_live_vm_fixtures.py` (add one assertion; existing ones stay)
- Overwrite: `scripts/live-vm/build-guest-image.sh`

- [ ] **Step 1: Add the failing key-resolution test**

The existing `test_build_is_idempotent_on_existing_image` already covers the
idempotency contract (it stays). Add a new test asserting that an explicitly-set but
missing `KDIVE_ROOTFS_AUTHORIZED_KEY` makes the builder fail fast with an actionable
message *before* requiring libguestfs — proving key resolution is wired and the
failure is deterministic without qemu/network. This cannot use `PATH=""` (the
destination does not exist, so the Stage-0 preflight runs and needs `realpath`/`mkdir`
from a real `PATH`); instead it keeps the inherited environment and points the key knob
at a non-existent file, which `resolve_authorized_key` returns and the `! -f` guard
rejects. Add `import os` to the imports, then append:

```python
def test_build_fails_fast_on_missing_authorized_key(tmp_path: Path) -> None:
    """A set-but-missing KDIVE_ROOTFS_AUTHORIZED_KEY fails fast before libguestfs.

    Proves key resolution is wired and the failure is deterministic without qemu/network:
    the build exits non-zero naming the key knob, not by stack-tracing on a missing tool.
    The inherited PATH is kept (the Stage-0 preflight needs realpath/mkdir); the key knob
    points at a non-existent file so resolution deterministically fails.
    """
    assert _BASH is not None, "bash is required"
    dest = tmp_path / "out" / "guest.qcow2"  # parent writable so the Stage-0 preflight passes
    dest.parent.mkdir()
    env = {**os.environ, "KDIVE_ROOTFS_AUTHORIZED_KEY": str(tmp_path / "missing.pub")}
    result = subprocess.run(
        [_BASH, str(_BUILD), str(dest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "KDIVE_ROOTFS_AUTHORIZED_KEY" in result.stderr
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_live_vm_fixtures.py::test_build_fails_fast_on_missing_authorized_key -q`
Expected: FAIL — the stub ignores the key knob and `qemu-img create`s the empty image,
exiting 0, so `assert result.returncode != 0` fails.

- [ ] **Step 3: Overwrite the builder**

Replace `scripts/live-vm/build-guest-image.sh` entirely with the content below. This
is the v1 `build-rootfs.sh` ported, with the five rewrite changes from spec §4:
(1) presence-only idempotency guard with an unambiguous no-op log; (2) Stage-0
output-dir preflight before any libguestfs tool; (3) `REPO_ROOT` two levels up
(`scripts/live-vm/` → repo root) and the `kdive.prereqs.managed_ssh_key` invocation;
(4) name retained; plus comment references retargeted to ADR-0052 / the runbook (no
phantom v1 ADR/doc references).

```bash
#!/usr/bin/env bash
# Build a bootable kdive-ready Fedora rootfs qcow2 — fully unprivileged (ADR-0052, #124).
#
# Two unprivileged libguestfs stages:
#   1. virt-builder customizes a Fedora scratch image (sshd, authorized key, a kdive-ready
#      serial unit that echoes the readiness marker to /dev/ttyS0).
#   2. virt-tar-out + virt-make-fs --type=ext4 repack the root tree into a no-partition-table
#      whole-disk ext4 qcow2 — the only layout the direct-kernel boot provider mounts
#      (root=/dev/vda, no initramfs, ADR-0030). /etc/fstab is then normalized to a lone "/"
#      entry and /etc/crypttab removed, because the scratch image's GPT-layout mount entries
#      would stall local-fs.target and the kdive-ready marker would never fire.
#
# Guest-internal SELinux is disabled (guest /etc/selinux/config) so the host-written
# authorized_keys is read without a relabel and the first boot does not relabel+reboot (which
# would risk a false boot timeout). This is the guest's internal SELinux only; it is independent
# of the host-side virt_image_t/0644 labeling of the image file, which still applies (see
# docs/runbooks/live-stack.md §3).
#
# Idempotent (presence-only): an existing file at the destination is left in place and the
# build is skipped — the destination is NOT validated and build inputs are NOT consulted, so a
# changed input (KDIVE_ROOTFS_DEBUG/_VMLINUX/_SSH_USER/_SIZE, or a rotated managed key) or a
# truncated image from an interrupted run is recovered by deleting the destination and re-running.
#
# No host-side sudo/pkexec. The output directory is pre-prepared by an OS admin for the default
# root-owned path (docs/runbooks/live-stack.md §3); the per-build write and final chmod 0644 are
# unprivileged. The chmod 0644 lets the separate qemu user read the image under qemu:///system.
set -euo pipefail

ROOTFS_PATH="${KDIVE_ROOTFS:-/var/lib/kdive/rootfs/minimal.qcow2}"
RELEASEVER="${KDIVE_ROOTFS_RELEASEVER:-43}"
# The Stage-1 `virt-builder --size` must be >= the template's virtual size, or Stage 1 fails
# with "images cannot be shrunk". 6G is the Fedora-43 floor; raise KDIVE_ROOTFS_SIZE for a
# larger template or when staging a vmlinux.
IMAGE_SIZE="${KDIVE_ROOTFS_SIZE:-6G}"
SSH_USER="${KDIVE_ROOTFS_SSH_USER:-root}"
# Debug-ready additions: install drgn (+ kexec-tools/makedumpfile) and, optionally, stage a
# paired vmlinux into the guest debug path so live drgn introspection is turnkey. Staging a
# vmlinux implies the debug tooling.
DEBUG_READY="${KDIVE_ROOTFS_DEBUG:-0}"
VMLINUX_PATH="${KDIVE_ROOTFS_VMLINUX:-}"
KERNEL_RELEASE="${KDIVE_ROOTFS_KERNEL_RELEASE:-}"
MARKER="kdive-ready"

# Repo root (scripts/live-vm/ -> repo root), so the managed-key helper is importable regardless
# of the caller's cwd. The helper is the single source of truth for the managed key path and its
# generation (ADR-0052), shared with the future connect-time `ssh -i` identity.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# The first positional argument overrides KDIVE_ROOTFS.
if [[ $# -ge 1 ]]; then
  ROOTFS_PATH="$1"
fi

# Idempotency guard (presence-only): runs first so a second invocation against an existing image
# is a no-op even with an empty PATH. It does not validate the file or consult build inputs. The
# `-f` test follows symlinks, so a symlink pointing at a regular file short-circuits here before
# the Stage-0 symlink refusal below — safe, because this branch performs no write.
if [[ -f "${ROOTFS_PATH}" ]]; then
  echo "rootfs image already present at ${ROOTFS_PATH}; leaving as-is (idempotent)." >&2
  echo "       No rebuild performed and build inputs were not consulted; delete the file to" >&2
  echo "       force a rebuild (e.g. after changing KDIVE_ROOTFS_DEBUG/_VMLINUX/_SSH_USER/_SIZE" >&2
  echo "       or rotating the managed SSH key)." >&2
  exit 0
fi

# Validate the guest username before it reaches a `virt-builder --run-command` guest shell or the
# colon-delimited `--ssh-inject "user:file:key"` selector. Restricting to the useradd NAME_REGEX
# envelope (lowercase-start, max 32) means the value cannot carry shell metacharacters or a stray
# ':' that would misparse the ssh-inject format.
if [[ ! "${SSH_USER}" =~ ^[a-z_][a-z0-9_-]*$ || ${#SSH_USER} -gt 32 ]]; then
  echo "error: KDIVE_ROOTFS_SSH_USER='${SSH_USER}' is not a valid username." >&2
  echo "       Allowed: ^[a-z_][a-z0-9_-]*\$, at most 32 characters." >&2
  exit 1
fi

# Stage-0 output-dir preflight (before any libguestfs tool, so a missing/unwritable output dir
# fails in seconds rather than after the minutes-long Stage 1). Refuse a pre-existing symlink at
# the output path so later writes cannot be redirected, then canonicalize the parent while keeping
# the final component literal. The path is operator-configurable, so it is not pinned under a fixed
# base.
if [[ -L "${ROOTFS_PATH}" ]]; then
  echo "error: KDIVE_ROOTFS='${ROOTFS_PATH}' is a symlink; refusing to write through it." >&2
  exit 1
fi
rootfs_parent="$(realpath -m -- "$(dirname -- "${ROOTFS_PATH}")")"
ROOTFS_PATH="${rootfs_parent}/$(basename -- "${ROOTFS_PATH}")"
if [[ ! -d "${rootfs_parent}" ]]; then
  if ! mkdir -p "${rootfs_parent}" 2>/dev/null; then
    echo "error: output directory '${rootfs_parent}' does not exist and could not be created." >&2
    echo "       Pre-prepare it (an OS admin step for the default root-owned path; see" >&2
    echo "       docs/runbooks/live-stack.md §3) or set KDIVE_ROOTFS to a writable location." >&2
    exit 1
  fi
fi
if [[ ! -w "${rootfs_parent}" ]]; then
  echo "error: output directory '${rootfs_parent}' is not writable by the current user." >&2
  echo "       Pre-prepare it (see docs/runbooks/live-stack.md §3) or set KDIVE_ROOTFS to a" >&2
  echo "       writable location." >&2
  exit 1
fi

# Validate the optional vmlinux staging inputs before any libguestfs tool is required, so the
# failure is deterministic in environments without virt-builder.
if [[ -n "${VMLINUX_PATH}" ]]; then
  DEBUG_READY=1 # staging a vmlinux is useless without drgn target-side
  if [[ -z "${KERNEL_RELEASE}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX is set but KDIVE_ROOTFS_KERNEL_RELEASE is empty." >&2
    echo "       Set KDIVE_ROOTFS_KERNEL_RELEASE to the booted kernel's \`make kernelrelease\`" >&2
    echo "       (== guest \`uname -r\`); the vmlinux is staged at" >&2
    echo "       /usr/lib/debug/lib/modules/<release>/vmlinux and a mismatch is never read." >&2
    exit 1
  fi
  if [[ ! "${KERNEL_RELEASE}" =~ ^[a-zA-Z0-9._+-]+$ || "${KERNEL_RELEASE}" == "." || "${KERNEL_RELEASE}" == ".." ]]; then
    echo "error: KDIVE_ROOTFS_KERNEL_RELEASE='${KERNEL_RELEASE}' is not a valid release." >&2
    echo "       Allowed: ^[a-zA-Z0-9._+-]+\$ (e.g. 7.0.0-kdive), excluding '.' and '..'." >&2
    exit 1
  fi
  if [[ -L "${VMLINUX_PATH}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX='${VMLINUX_PATH}' is a symlink; refusing to stage it." >&2
    exit 1
  fi
  if [[ ! -f "${VMLINUX_PATH}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX='${VMLINUX_PATH}' is not a regular file." >&2
    exit 1
  fi
  VMLINUX_PATH="$(realpath -m -- "${VMLINUX_PATH}")"
  if [[ -z "${KDIVE_ROOTFS_SIZE:-}" ]]; then
    vmlinux_mib=$((($(stat -c %s -- "${VMLINUX_PATH}") + 1048575) / 1048576))
    recommended_gib=$((6 + (vmlinux_mib * 12 / 10 + 1023) / 1024))
    echo "warning: staging a ${vmlinux_mib} MiB vmlinux at the default KDIVE_ROOTFS_SIZE=6G;" >&2
    echo "         this is likely too small. Set KDIVE_ROOTFS_SIZE to at least ${recommended_gib}G" >&2
    echo "         (6G base + vmlinux + ext4 overhead) to avoid a virt-make-fs 'too small' error." >&2
  fi
fi

resolve_authorized_key() {
  if [[ -n "${KDIVE_ROOTFS_AUTHORIZED_KEY:-}" ]]; then
    printf '%s\n' "${KDIVE_ROOTFS_AUTHORIZED_KEY}"
    return
  fi
  # Single source of truth for the managed key path + generation (ADR-0052). The helper ensures
  # the keypair and prints the .pub path on stdout; it names KDIVE_ROOTFS_AUTHORIZED_KEY on stderr
  # if generation fails. `|| true` is load-bearing under `set -e`: a non-zero exit in the command
  # substitution would otherwise abort before the explanatory guard below runs.
  PYTHONPATH="${REPO_ROOT}/src" python3 -m kdive.prereqs.managed_ssh_key --ensure-public-key \
    || true
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: required command '$1' not found on PATH (install libguestfs-tools)" >&2
    exit 1
  }
}

authorized_key="$(resolve_authorized_key)"
if [[ -z "${authorized_key}" || ! -f "${authorized_key}" ]]; then
  echo "error: could not resolve an SSH public key to install." >&2
  echo "       Set KDIVE_ROOTFS_AUTHORIZED_KEY to a .pub file, or ensure 'ssh-keygen' and" >&2
  echo "       'python3' are available so kdive can generate its managed key." >&2
  exit 1
fi

require virt-builder
require virt-tar-out
require virt-make-fs
require guestfish
require qemu-img

if ! virt-builder --list 2>/dev/null | grep -qE "^fedora-${RELEASEVER}[[:space:]]"; then
  echo "error: template 'fedora-${RELEASEVER}' is not in the libguestfs index." >&2
  echo "       Run 'virt-builder --list' to see available releases and set" >&2
  echo "       KDIVE_ROOTFS_RELEASEVER to one of them. (First use fetches the template over the" >&2
  echo "       network; ensure reachability or a pre-seeded virt-builder cache.)" >&2
  exit 1
fi

# mktemp creates each file 0600 regardless of umask, and the `>`-redirect writes below preserve
# that mode. scratch and rootfs_tar are written by external tools (virt-builder --output /
# virt-tar-out) that may unlink+recreate at the default umask; they are chmod'd 0600 right after
# each tool write so their mode is deterministic regardless of tool behavior.
unit_file="$(mktemp)"
fstab_file="$(mktemp)"
selinux_file="$(mktemp)"
scratch="$(mktemp --suffix=.qcow2)"
rootfs_tar="$(mktemp --suffix=.tar)"
cleanup() { rm -f "${unit_file}" "${fstab_file}" "${selinux_file}" "${scratch}" "${rootfs_tar}"; }
trap cleanup EXIT

cat >"${unit_file}" <<EOF
[Unit]
Description=Signal kdive serial readiness
After=dev-ttyS0.device
Wants=dev-ttyS0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo ${MARKER} > /dev/ttyS0'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

printf '/dev/vda / ext4 defaults 0 1\n' >"${fstab_file}"
printf 'SELINUX=disabled\nSELINUXTYPE=targeted\n' >"${selinux_file}"

# virt-builder runs --run-command and --ssh-inject in command-line order, and --ssh-inject
# requires the user to already exist. The useradd --run-command is therefore placed before
# --ssh-inject so a non-root SSH_USER exists when the key is injected.
builder_args=(
  "fedora-${RELEASEVER}"
  --format qcow2 --size "${IMAGE_SIZE}" --output "${scratch}"
  --install openssh-server
  --run-command 'systemctl enable sshd.service'
)
if [[ "${SSH_USER}" != "root" ]]; then
  builder_args+=(--run-command "useradd --create-home --shell /bin/bash ${SSH_USER}")
fi
builder_args+=(
  --ssh-inject "${SSH_USER}:file:${authorized_key}"
  --upload "${unit_file}:/etc/systemd/system/${MARKER}.service"
  --run-command "systemctl enable ${MARKER}.service"
)
if [[ "${DEBUG_READY}" == "1" ]]; then
  builder_args+=(--install "drgn,kexec-tools,makedumpfile")
fi
if [[ -n "${VMLINUX_PATH}" ]]; then
  guest_debug_dir="/usr/lib/debug/lib/modules/${KERNEL_RELEASE}"
  builder_args+=(
    --mkdir "${guest_debug_dir}"
    --upload "${VMLINUX_PATH}:${guest_debug_dir}/vmlinux"
  )
fi

echo "Stage 1: customizing fedora-${RELEASEVER} scratch image ..." >&2
virt-builder "${builder_args[@]}"
chmod 0600 "${scratch}"

echo "Stage 2: repacking to whole-disk ext4 ${ROOTFS_PATH} ..." >&2
virt-tar-out -a "${scratch}" / "${rootfs_tar}"
chmod 0600 "${rootfs_tar}"
virt-make-fs --type=ext4 --format=qcow2 --size="${IMAGE_SIZE}" "${rootfs_tar}" "${ROOTFS_PATH}"

# Normalize the inherited mount config and disable guest-internal SELinux. The GFEOF delimiter is
# intentionally UNQUOTED: ${fstab_file}/${selinux_file} are host-side temp paths that must expand
# so guestfish receives real filenames. The guest-side paths are fixed literals.
guestfish --rw -a "${ROOTFS_PATH}" -i <<GFEOF
upload ${fstab_file} /etc/fstab
upload ${selinux_file} /etc/selinux/config
rm-f /etc/crypttab
GFEOF

# The caller owns the file it just wrote; chmod is unprivileged. 0644 lets the separate qemu user
# read the image under qemu:///system. Re-assert the target is a regular file (not a symlink
# swapped in during the build) before chmod redirects onto it.
if [[ ! -f "${ROOTFS_PATH}" || -L "${ROOTFS_PATH}" ]]; then
  echo "error: ${ROOTFS_PATH} is not a regular file after build; refusing to chmod." >&2
  exit 1
fi
chmod 0644 "${ROOTFS_PATH}"

echo "Done: ${ROOTFS_PATH}" >&2
qemu-img info "${ROOTFS_PATH}" >&2 || true
# Print the content hash on stdout so a future publish/upload flow can content-address this
# artifact (spec §7).
sha256sum -- "${ROOTFS_PATH}" || true
```

- [ ] **Step 4: Run the fixtures suite to verify all pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_fixtures.py -q`
Expected: PASS — `test_script_parses[build-guest-image]` (valid `bash -n`),
`test_script_declares_strict_mode[build-guest-image]`,
`test_build_is_idempotent_on_existing_image` (existing image → exit 0, `idempotent`
in stderr), and the new `test_build_fails_fast_on_missing_authorized_key`
(`KDIVE_ROOTFS_AUTHORIZED_KEY` named, non-zero exit). The `fetch-kernel-tree`
parametrizations stay green.

- [ ] **Step 5: Shell lint + format check**

Run: `shellcheck scripts/live-vm/build-guest-image.sh && shfmt -i 2 -d scripts/live-vm/build-guest-image.sh`
Expected: shellcheck clean; `shfmt -d` prints no diff. If `shfmt` shows a diff, apply
it (`shfmt -i 2 -w scripts/live-vm/build-guest-image.sh`) and re-run both.

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/build-guest-image.sh tests/scripts/test_live_vm_fixtures.py
git commit -m "feat(live-vm): port the bootable rootfs builder, replacing the stub

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Document host-side image labeling and correct the runbook

**Files:**
- Modify: `docs/runbooks/live-stack.md` (§3)

- [ ] **Step 1: Correct the §3 builder description and add the labeling note**

In `docs/runbooks/live-stack.md` §3, the line `scripts/live-vm/build-guest-image.sh
# writes the kdump-enabled guest image` is now wrong (the builder writes a
kdive-ready rootfs, not a kdump image). Replace that inline comment and add a
host-side image-labeling note immediately after the code block. Change:

```
scripts/live-vm/build-guest-image.sh    # writes the kdump-enabled guest image
```

to:

```
scripts/live-vm/build-guest-image.sh    # builds the bootable kdive-ready rootfs qcow2
```

Then, immediately after the closing ``` of that code block (before the "Point
`KDIVE_GUEST_IMAGE` …" paragraph), insert:

```markdown
The builder runs unprivileged and writes the rootfs to `KDIVE_ROOTFS` (default
`/var/lib/kdive/rootfs/minimal.qcow2`). For the default root-owned path, an OS admin
pre-prepares the output directory once and makes it writable by the build user; the
per-build write and the final `chmod 0644` are unprivileged. The image is left `0644` so
the separate `qemu` user can read it under `qemu:///system`. Under SELinux the file also
needs the `virt_image_t` label (the standard label for libvirt-managed images); this is the
host-side file label and is independent of the guest-internal SELinux the builder disables.
The build is idempotent on the destination path — delete the file to force a rebuild after
changing any build input (`KDIVE_ROOTFS_DEBUG`, `KDIVE_ROOTFS_VMLINUX`,
`KDIVE_ROOTFS_SSH_USER`, `KDIVE_ROOTFS_SIZE`, or a rotated managed SSH key).
```

- [ ] **Step 2: Verify the docs guardrails pass**

Run: `just docs-check && just check-mermaid`
Expected: PASS (no broken links / markdown issues introduced). If `docs-check`
reports a dead relative link, fix the link target and re-run.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/live-stack.md
git commit -m "docs(runbook): correct the builder description; add image-labeling note

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Full guardrail sweep

**Files:** none (verification only)

- [ ] **Step 1: Run the hard-gating CI recipes locally**

Run: `just lint && just type && just lint-shell && just test`
Expected: all green. `just lint` = `ruff check` + `ruff format --check`; `just type`
= whole-tree `ty check`; `just lint-shell` = shellcheck over the scripts; `just test`
= the non-gated suite (the new `tests/prereqs` + `tests/scripts` tests included, the
`live_vm` build itself not run).

- [ ] **Step 2: Confirm no stray stub references remain**

Run: `rg -n "qemu-img create|sha256:0000|kdump scaffold" scripts/live-vm/build-guest-image.sh`
Expected: no matches (the stub content is fully gone).

- [ ] **Step 3: Mark the plan tasks complete and proceed to the diff review loop**

No commit. Hand back to `/work-issue` step 6 (adversarial-review `main..HEAD`).

---

## Notes on what is intentionally NOT here

- The **real** `virt-builder` build and the §6.1 boot-to-marker acceptance are
  host-and-network-dependent and run manually (spec §6.1), not in `just test`. Do not
  add a non-gated test that invokes `virt-builder`/`qemu`.
- No catalog-schema change, no boot/readiness consumption seam, no SSH transport — those
  are sibling gaps of #123 (spec §2 non-goals).
