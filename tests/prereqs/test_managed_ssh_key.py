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
    assert managed_key_dir(env={**base, "XDG_DATA_HOME": ""}) == Path(
        "/home/u/.local/share/kdive/ssh"
    )
    assert managed_key_dir(env={**base, "XDG_DATA_HOME": "rel/path"}) == Path(
        "/home/u/.local/share/kdive/ssh"
    )


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
    assert managed_private_key_path(env=env) == Path(
        "/home/u/.local/share/kdive/ssh/id_kdive_ed25519"
    )
    assert managed_public_key_path(env=env) == Path(
        "/home/u/.local/share/kdive/ssh/id_kdive_ed25519.pub"
    )


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


def test_run_keygen_wraps_non_filenotfound_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An ssh-keygen on PATH that is present-but-not-executable surfaces as PermissionError,
    # not FileNotFoundError; it must still become a ManagedKeyError so the CLI handler catches it.
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("permission denied")

    monkeypatch.setattr("kdive.prereqs.managed_ssh_key.subprocess.run", _boom)
    with pytest.raises(ManagedKeyError, match="ssh-keygen"):
        ensure_managed_keypair(env={"HOME": str(tmp_path)})


# --- CLI -------------------------------------------------------------------------------


def _hermetic_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    # main() reads os.environ; managed_key_dir prefers KDIVE_SSH_KEY_DIR / an absolute
    # XDG_DATA_HOME over the HOME default, so an ambient value would redirect the path and
    # break the assertions. Pin HOME and clear the two overrides for a host-independent test.
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
