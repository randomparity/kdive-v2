"""The structural KDIVE_* drift guard (ADR-0087 decision 6).

The guard must resolve module-level string constants and flag dynamic args, not just
match string literals — most real reads go through a constant (``_X_ENV = "KDIVE_..."``)
or a generic ``os.environ.get(name)`` helper.
"""

from __future__ import annotations

from pathlib import Path

from scripts.config_env_guard import find_violations


def _vars(hits) -> set[str | None]:
    return {v.variable for v in hits}


def test_flags_each_access_form(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text(
        "import os\n"
        "a = os.environ.get('KDIVE_X')\n"
        "b = os.environ['KDIVE_Y']\n"
        "c = os.getenv('KDIVE_Z')\n"
    )
    assert _vars(find_violations([f], allowlist=set())) == {"KDIVE_X", "KDIVE_Y", "KDIVE_Z"}


def test_resolves_module_level_constant(tmp_path: Path) -> None:
    # The dominant real pattern: a constant holds the name, the read uses the constant.
    f = tmp_path / "const.py"
    f.write_text("import os\n_DB_ENV = 'KDIVE_DATABASE_URL'\nx = os.environ.get(_DB_ENV)\n")
    assert _vars(find_violations([f], allowlist=set())) == {"KDIVE_DATABASE_URL"}


def test_flags_dynamic_unresolvable_arg(tmp_path: Path) -> None:
    # A generic helper reading whatever name it is handed cannot be proven non-KDIVE,
    # so the access form itself is flagged (must route through the registry).
    f = tmp_path / "helper.py"
    f.write_text("import os\ndef e(name):\n    return os.environ.get(name)\n")
    hits = find_violations([f], allowlist=set())
    assert len(hits) == 1
    assert hits[0].variable is None  # dynamic


def test_non_kdive_literal_is_ignored(tmp_path: Path) -> None:
    f = tmp_path / "fine.py"
    f.write_text("import os\nx = os.environ.get('HOME')\ny = os.environ['PATH']\n")
    assert find_violations([f], allowlist=set()) == []


def test_non_kdive_constant_is_ignored(tmp_path: Path) -> None:
    f = tmp_path / "fine2.py"
    f.write_text("import os\n_HOME = 'HOME'\nx = os.environ.get(_HOME)\n")
    assert find_violations([f], allowlist=set()) == []


def test_environ_spread_is_not_matched(tmp_path: Path) -> None:
    # Forwarding the whole environment to a subprocess is not a named-variable read.
    f = tmp_path / "spread.py"
    f.write_text("import os\nenv = {**os.environ, 'LC_ALL': 'C'}\n")
    assert find_violations([f], allowlist=set()) == []


def test_local_variable_get_is_not_matched(tmp_path: Path) -> None:
    # `environ = os.environ; environ.get(...)` reads through a local, not os.environ;
    # this is the managed_ssh_key stdlib-only pattern (allowlisted separately).
    f = tmp_path / "local.py"
    f.write_text("import os\nenviron = os.environ\nx = environ.get('KDIVE_X')\n")
    assert find_violations([f], allowlist=set()) == []


def test_allowlisted_file_is_skipped(tmp_path: Path) -> None:
    f = tmp_path / "ok.py"
    f.write_text("import os\nx = os.environ.get('KDIVE_X')\n")
    assert find_violations([f], allowlist={f}) == []
