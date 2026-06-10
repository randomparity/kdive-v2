"""Structural no-bypass guard: the whole ``kdive.cli.*`` package avoids services + creds.

ADR-0089 decision 5: the operator host holds only the bearer token and the server URL.
This guard enforces that boundary two ways so it cannot erode silently:

- An AST walk over every ``src/kdive/cli/*.py`` file rejects any ``kdive.services``
  import regardless of binding scope — a *function-local* ``from kdive.services import x``
  inside a future verb is caught, not just a module-top-level import.
- A runtime allowlist check confirms each :class:`Setting` reachable from a cli module is
  one the CLI is permitted to read; a new credential setting added to the registry later
  cannot be referenced without tripping this, because it is an allowlist (safe default),
  not a denylist of known-bad names. The two ``KDIVE_OIDC_*`` discovery settings the
  ``login`` verb resolves the mock-OIDC issuer from are non-secret OIDC metadata (issuer URL
  and audience), not credentials, so they are allowlisted alongside the URL/token/client-id
  the operator host already holds (ADR-0089 decision 5).
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import kdive.cli
from kdive.config.cli_settings import CLI_CLIENT_ID, SERVER_URL, TOKEN
from kdive.config.core_settings import OIDC_AUDIENCE, OIDC_ISSUER
from kdive.config.registry import Setting

_CLI_DIR = Path(kdive.cli.__path__[0])
_ALLOWED_SETTING_NAMES = {
    SERVER_URL.name,
    TOKEN.name,
    CLI_CLIENT_ID.name,
    OIDC_ISSUER.name,
    OIDC_AUDIENCE.name,
}


def _walk_cli_modules() -> list[str]:
    names = [kdive.cli.__name__]
    for info in pkgutil.walk_packages(kdive.cli.__path__, kdive.cli.__name__ + "."):
        names.append(info.name)
    return names


def _imports_kdive_services(source: str) -> bool:
    """Return True if ``source`` imports ``kdive.services`` at any scope (AST walk)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "kdive.services" or module.startswith("kdive.services."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "kdive.services" or alias.name.startswith("kdive.services."):
                    return True
    return False


def test_no_cli_source_imports_kdive_services() -> None:
    offenders = [
        path.relative_to(_CLI_DIR).as_posix()
        for path in sorted(_CLI_DIR.rglob("*.py"))
        if _imports_kdive_services(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"kdive.cli imports kdive.services in: {offenders}"


def test_cli_imports_no_services_module() -> None:
    for name in _walk_cli_modules():
        module = importlib.import_module(name)
        for attr in dir(module):
            obj = getattr(module, attr)
            origin = getattr(obj, "__module__", "")
            assert not origin.startswith("kdive.services"), (
                f"{name} pulls in kdive.services via {attr}"
            )


def test_cli_references_only_allowlisted_settings() -> None:
    for name in _walk_cli_modules():
        module = importlib.import_module(name)
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, Setting):
                assert obj.name in _ALLOWED_SETTING_NAMES, (
                    f"{name} references non-allowlisted setting {obj.name} via {attr}"
                )
