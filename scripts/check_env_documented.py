"""Coverage guard: every ``KDIVE_*`` token in the tree is documented (config.md SoT).

Companion to ``scripts/config_env_guard.py``. That guard is an *access-form* rule (no
``os.environ`` read of a ``KDIVE_*`` key outside ``kdive.config``) and is AST-based, so it only
sees Python. This guard is a *coverage* rule: it sweeps every file under ``src/ tests/ scripts/
deploy/`` for ``KDIVE_[A-Z0-9_]+`` tokens — including bash, YAML, and prose an AST cannot parse —
and fails if any token is neither a registry setting (:func:`kdive.config.all_settings`) nor a
catalogued non-registry variable (:mod:`kdive.config.external_env`) nor an explicitly-ignored
non-env token.

A token sweep is deliberately broad, so it needs two precision filters:

* tokens ending in ``_`` are glob/prefix fragments (``KDIVE_S3_*``, ``KDIVE_OTEL_*`` in prose) and
  are skipped — no real variable name ends in ``_``;
* ``_NOT_ENV`` ignores tokens that match the pattern but are not env reads (metadata-namespace and
  secret-ref constants, a config-validation message token, and the retired
  ``KDIVE_REMOTE_LIBVIRT_*`` inventory singletons that ``tests/guards/`` asserts stay absent);
* ``_EXCLUDE_FILES`` skips the config/registry/guard-machinery test files, whose synthetic
  single-letter fixtures exist only to exercise the config system itself.

Stdlib only, so CI runs it without a synced env. Exit 0 clean, 1 on undocumented tokens.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from kdive.config import all_settings
from kdive.config.external_env import external_env_names

_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("src", "tests", "scripts", "deploy")
_TOKEN = re.compile(r"KDIVE_[A-Z0-9_]+")

# Tokens that match the pattern but are not environment-variable reads.
_NOT_ENV: frozenset[str] = frozenset(
    {
        # Non-env constants in product code.
        "KDIVE_METADATA_NS",  # object-store metadata namespace prefix
        "KDIVE_PROVIDER_CA",  # secret-ref label, not an env var
        "KDIVE_ROOTFS_AUTHORIZED_KEY",  # secret-ref label, not an env var
        # A config-validation error message token in tests/mcp/core/test_app.py.
        "KDIVE_S3_ENDPOINT",
        # Retired remote-libvirt inventory singletons (M2.6 #395); tests/guards/ asserts they
        # never reappear in code, so they are intentionally undocumented and must stay ignored.
        "KDIVE_REMOTE_LIBVIRT_URI",
        "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",
        "KDIVE_REMOTE_LIBVIRT_GDB_ADDR",
        "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN",
        "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MAX",
        "KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP",
    }
)

# Test files whose ``KDIVE_*`` tokens are synthetic fixtures used to exercise the config system
# itself (registry/guard/generator/actor), not real variables.
_EXCLUDE_FILES: frozenset[Path] = frozenset(
    _ROOT / rel
    for rel in (
        "tests/config/test_registry.py",
        "tests/scripts/test_config_env_guard.py",
        "tests/scripts/test_gen_config_reference.py",
        "tests/scripts/test_check_env_documented.py",
        "tests/security/test_actor.py",
    )
)


@dataclass(frozen=True)
class Undocumented:
    file: Path
    line: int
    token: str


def documented_names() -> frozenset[str]:
    """The full documented set: registry settings ∪ catalogued non-registry variables."""
    return frozenset(s.name for s in all_settings()) | external_env_names()


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for sub in _SCAN_DIRS:
        files.extend(sorted((_ROOT / sub).rglob("*")))
    return [f for f in files if f.is_file() and f not in _EXCLUDE_FILES]


def find_undocumented(files: list[Path], documented: frozenset[str]) -> list[Undocumented]:
    """Return every undocumented ``KDIVE_*`` token occurrence across ``files``."""
    out: list[Undocumented] = []
    allowed = documented | _NOT_ENV
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _TOKEN.finditer(line):
                token = match.group(0)
                if token.endswith("_") or token in allowed:
                    continue
                out.append(Undocumented(path, lineno, token))
    return out


def main() -> int:
    documented = documented_names()
    undocumented = find_undocumented(_scan_files(), documented)
    seen: set[str] = set()
    for item in undocumented:
        rel = item.file.relative_to(_ROOT)
        print(f"{rel}:{item.line}: {item.token} is not documented", file=sys.stderr)
        seen.add(item.token)
    if undocumented:
        print(
            f"{len(seen)} undocumented KDIVE_* variable(s); add a registry Setting, an "
            "external_env.py entry, or an explicit _NOT_ENV ignore",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
