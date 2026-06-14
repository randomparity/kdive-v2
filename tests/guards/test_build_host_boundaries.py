"""Guard shared build-host dispatch against provider implementation imports."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"


def test_build_host_dispatch_does_not_import_remote_libvirt() -> None:
    text = (SRC / "providers" / "build_host" / "dispatch.py").read_text(encoding="utf-8")
    assert "providers.remote_libvirt" not in text
