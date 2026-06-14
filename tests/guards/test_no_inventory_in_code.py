"""Guard: image/inventory definitions must live in ``systems.toml``, not in code (ADR-0112).

Phase 1 (#392) removes every in-code image definition — the ``images/seed_data`` rootfs YAML
tree, the inline rootfs/manifest YAML in ``admin/default_fixtures.py``, and the
``REMOTE_BASE_IMAGE_NAME`` literal — so the catalog is sourced only from the reconciled
``systems.toml`` ``[[image]]`` entries. Phase 3 (#395) removes the singleton
``KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR,ALLOCATION_CAP,GDB_PORT_*}`` connection env vars —
the remote connection identity now comes from the ``[[remote_libvirt]]`` instance. This test pins
those deletions so the definitions cannot silently return to code.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"

# The connection-identity singletons deleted in Phase 3 (#395). The libvirt host knobs the v2
# inventory model does not carry — STORAGE_POOL / NETWORK / MACHINE — remain legitimate env
# settings and are intentionally absent from this list.
_DELETED_REMOTE_SINGLETONS = (
    "KDIVE_REMOTE_LIBVIRT_URI",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",
    "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
    "KDIVE_REMOTE_LIBVIRT_GDB_ADDR",
    "KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP",
    "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN",
    "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MAX",
)


def test_no_seed_data_tree() -> None:
    assert not (SRC / "images" / "seed_data").exists()


def test_no_inline_rootfs_yaml_in_fixtures() -> None:
    text = (SRC / "admin" / "default_fixtures.py").read_text(encoding="utf-8")
    assert "rootfs/fedora-kdive-ready" not in text
    assert "schema_version: 1" not in text  # the embedded manifest YAML


def test_no_remote_base_image_literal() -> None:
    text = (SRC / "providers" / "remote_libvirt" / "rootfs_build.py").read_text(encoding="utf-8")
    assert "fedora-kdive-remote-base-43" not in text
    assert "REMOTE_BASE_IMAGE_NAME" not in text


def test_no_remote_libvirt_singleton_env_reads() -> None:
    provider_dir = SRC / "providers" / "remote_libvirt"
    offenders: list[str] = []
    for py in provider_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for name in _DELETED_REMOTE_SINGLETONS:
            if name in text:
                offenders.append(f"{py.relative_to(SRC)}: {name}")
    assert not offenders, (
        "deleted KDIVE_REMOTE_LIBVIRT_* connection singletons must not reappear under "
        f"providers/remote_libvirt/ (ADR-0112, #395); found: {offenders}"
    )
