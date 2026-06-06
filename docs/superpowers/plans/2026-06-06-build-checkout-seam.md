# Build checkout seam (#125) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `build.py:_real_checkout`'s `MISSING_DEPENDENCY` stub with a real warm-tree checkout — rsync the operator's `KDIVE_KERNEL_SRC` into the per-Run workspace, stage the profile's `.config`, and apply its optional patch — with a clean categorized error contract.

**Architecture:** Decompose into four module-level helpers (`_resolve_local_ref`, `_stage_config`, `_apply_patch`, `_sync_tree`) that `_real_checkout` composes in order. Every branch is unit-tested host-free by mocking `subprocess.run`/`shutil.which`; the composition's ordering is tested by monkeypatching the three step helpers; only the real-`make` end-to-end assertion stays `live_vm`-gated. No code needs a `# pragma: no cover` because the mocked tests execute every branch.

**Tech Stack:** Python 3.13, `subprocess` (rsync, `git apply`), `urllib.parse.urlsplit`, `kdive.security.redaction.Redactor`, `pytest` (`uv run python -m pytest`).

**Spec:** [`../specs/2026-06-06-build-checkout-seam-design.md`](../specs/2026-06-06-build-checkout-seam-design.md) · **ADR:** [`../../adr/0053-build-checkout-seam.md`](../../adr/0053-build-checkout-seam.md)

---

## File Structure

- **Modify** `src/kdive/providers/local_libvirt/build.py`:
  - new imports: `shutil`, `from urllib.parse import urlsplit`, `from kdive.security.redaction import Redactor`.
  - new module constant `_STDERR_TAIL = 2000`.
  - new helpers `_redacted_tail`, `_resolve_local_ref`, `_stage_config`, `_apply_patch`, `_sync_tree`.
  - rewrite `_real_checkout` to compose them (drop its `# pragma: no cover - live_vm`).
  - export the new helpers in `__all__` (so tests import them from the module surface).
- **Modify** `tests/providers/local_libvirt/test_build.py`:
  - host-free unit tests for each helper + the composition; fill the `live_vm` real-make stub.

All changes are additive within one source file and one test file; nothing else in `build()` or the profile/store changes.

---

## Task 1: `_redacted_tail` + `_resolve_local_ref`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py` (imports near line 17-32; helpers after `_real_read_build_id`, ~line 284)
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/local_libvirt/test_build.py` (and add `from kdive.providers.local_libvirt.build import _resolve_local_ref` to the existing import block):

```python
# --- _resolve_local_ref -------------------------------------------------------------


def test_resolve_local_ref_file_url(tmp_path: Path) -> None:
    target = tmp_path / "x.config"
    target.write_text("CONFIG_X=y\n")
    assert _resolve_local_ref(f"file://{target}", kind="config_ref") == target


def test_resolve_local_ref_bare_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "x.config"
    target.write_text("CONFIG_X=y\n")
    assert _resolve_local_ref(str(target), kind="config_ref") == target


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.com/x.config",
        "git+https://example.com/x#v1",
        "s3://bucket/x.config",
    ],
)
def test_resolve_local_ref_rejects_non_local_scheme(ref: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(ref, kind="config_ref")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_file_url_with_netloc() -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref("file://host/path/x.config", kind="config_ref")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_relative_path() -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref("configs/x.config", kind="config_ref")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(str(tmp_path / "absent.config"), kind="config_ref")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(str(tmp_path), kind="config_ref")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k resolve_local_ref -q`
Expected: FAIL — `ImportError: cannot import name '_resolve_local_ref'`.

- [ ] **Step 3: Add imports and the helpers**

In the import block of `src/kdive/providers/local_libvirt/build.py`, add `shutil` (alphabetical, after `os`), `from urllib.parse import urlsplit`, and `from kdive.security.redaction import Redactor`. After `parse_gnu_build_id`'s constants area add the constant `_STDERR_TAIL = 2000`. After `_real_read_build_id` (~line 284) add:

```python
def _redacted_tail(text: str) -> str:
    """Redact known secrets/`key=value` pairs, then return the trailing ``_STDERR_TAIL`` chars."""
    return Redactor().redact_text(text)[-_STDERR_TAIL:]


def _resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref (``config_ref``/``patch_ref``) to an existing local file.

    Accepts a ``file:///abs/path`` URL (empty authority) or a bare absolute path. Rejects a
    non-local scheme, a ``file://`` URL with a host, a non-absolute path, or a path that is
    not an existing regular file.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (with ``details={"kind": kind}``; the
            submitted ref value is never echoed) for any unsupported or unresolvable ref.
    """
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise _ref_error(kind, "config/patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise _ref_error(kind, "config/patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise _ref_error(kind, "config/patch ref must be an absolute path")
    if not path.is_file():
        raise _ref_error(kind, "config/patch ref does not resolve to a readable file")
    return path


def _ref_error(kind: str, message: str) -> CategorizedError:
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k resolve_local_ref -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): resolve local config/patch refs for the checkout seam"
```

---

## Task 2: `_stage_config`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write the failing tests**

Add `_stage_config` to the test import block, then append:

```python
# --- _stage_config ------------------------------------------------------------------


def test_stage_config_copies_bytes_to_workspace_dotconfig(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = tmp_path / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    _stage_config(str(config), workspace)

    assert (workspace / ".config").read_text() == "CONFIG_FROM_REF=y\n"


def test_stage_config_overwrites_existing_dotconfig(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".config").write_text("CONFIG_WARM_TREE=y\n")
    config = tmp_path / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    _stage_config(str(config), workspace)

    assert (workspace / ".config").read_text() == "CONFIG_FROM_REF=y\n"


def test_stage_config_missing_ref_is_configuration_error(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(CategorizedError) as caught:
        _stage_config(str(tmp_path / "absent.config"), workspace)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k stage_config -q`
Expected: FAIL — `ImportError: cannot import name '_stage_config'`.

- [ ] **Step 3: Add the helper**

After `_resolve_local_ref`/`_ref_error` in `build.py`:

```python
def _stage_config(config_ref: str, workspace: Path) -> None:
    """Copy the resolved ``config_ref`` to ``workspace/.config`` (overwriting any existing one)."""
    source = _resolve_local_ref(config_ref, kind="config_ref")
    shutil.copyfile(source, workspace / ".config")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k stage_config -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): stage the profile .config into the per-Run workspace"
```

---

## Task 3: `_apply_patch`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write the failing tests**

Add `_apply_patch` to the test import block and `import shutil` at the top of the test file (if not present). Append:

```python
# --- _apply_patch -------------------------------------------------------------------

_GOOD_PATCH = (
    "--- a/init/main.c\n"
    "+++ b/init/main.c\n"
    "@@ -1,2 +1,2 @@\n"
    " line1\n"
    "-line2\n"
    "+line2-patched\n"
)
_BAD_PATCH = (
    "--- a/init/main.c\n"
    "+++ b/init/main.c\n"
    "@@ -1,2 +1,2 @@\n"
    " nomatch1\n"
    "-nomatch2\n"
    "+nomatch3\n"
)


def _workspace_with_target(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "init").mkdir(parents=True)
    (workspace / "init" / "main.c").write_text("line1\nline2\n")
    return workspace


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_apply_patch_applies_clean_diff(tmp_path: Path) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)

    _apply_patch(str(patch), workspace)

    assert (workspace / "init" / "main.c").read_text() == "line1\nline2-patched\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_apply_patch_bad_diff_is_configuration_error_with_redacted_detail(tmp_path: Path) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "bad.patch"
    patch.write_text(_BAD_PATCH)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "stderr" in caught.value.details
    # the raw added patch line is never echoed back through the error detail
    assert "nomatch3" not in caught.value.details["stderr"]


def test_apply_patch_missing_git_is_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: None)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
```

Add `from kdive.providers.local_libvirt import build as build_module` to the test imports (used to monkeypatch `build_module.shutil.which`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k apply_patch -q`
Expected: FAIL — `ImportError: cannot import name '_apply_patch'`.

- [ ] **Step 3: Add the helper**

After `_stage_config`:

```python
def _apply_patch(patch_ref: str, workspace: Path) -> None:
    """Apply the resolved ``patch_ref`` to the workspace tree with ``git apply -p1``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the ref is unresolvable (via
            :func:`_resolve_local_ref`) or the patch does not apply (a redacted stderr tail
            is placed in ``details``); ``MISSING_DEPENDENCY`` if ``git`` is absent.
    """
    patch = _resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "apply", "-p1", str(patch)],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": _redacted_tail(result.stderr)},
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k apply_patch -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): apply the profile patch_ref with git apply (categorized)"
```

---

## Task 4: `_sync_tree`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write the failing tests**

Add `_sync_tree` to the test import block. Append:

```python
# --- _sync_tree ---------------------------------------------------------------------


def _ok_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_sync_tree_missing_kernel_src_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _sync_tree("", tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_nonexistent_kernel_src_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(tmp_path / "absent"), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_missing_rsync_is_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: None)
    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_sync_tree_creates_workspace_and_invokes_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    workspace = tmp_path / "runs" / "abc" / "ws"  # parents do not exist yet
    calls: list[list[str]] = []

    def _record(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok_run()

    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(build_module.subprocess, "run", _record)

    _sync_tree(str(src), workspace)

    assert workspace.is_dir()  # mkdir(parents=True) ran before rsync
    assert calls == [["rsync", "-a", "--delete", f"{src}/", f"{workspace}/"]]


def test_sync_tree_rsync_nonzero_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()

    def _fail(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=23, stdout="", stderr="rsync: disk full")

    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(build_module.subprocess, "run", _fail)

    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "stderr" in caught.value.details
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k sync_tree -q`
Expected: FAIL — `ImportError: cannot import name '_sync_tree'`.

- [ ] **Step 3: Add the helper**

After `_apply_patch`:

```python
def _sync_tree(kernel_src: str, workspace: Path) -> None:
    """Mirror the warm ``kernel_src`` tree into ``workspace`` with ``rsync -a --delete``.

    Creates ``workspace`` (and missing parents) first, since ``build()`` does not and rsync
    does not create missing parent directories.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``kernel_src`` is empty or not a
            directory; ``MISSING_DEPENDENCY`` if ``rsync`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on a non-zero rsync exit (redacted stderr in details).
    """
    if not kernel_src or not Path(kernel_src).is_dir():
        raise CategorizedError(
            "KDIVE_KERNEL_SRC is not set to an existing kernel source tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("rsync") is None:
        raise CategorizedError(
            "rsync is required to materialize the warm kernel tree",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    workspace.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["rsync", "-a", "--delete", f"{kernel_src.rstrip('/')}/", f"{workspace}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": _redacted_tail(result.stderr)},
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k sync_tree -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): rsync the warm kernel tree into the per-Run workspace"
```

---

## Task 5: Compose `_real_checkout` + wiring test

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py:243-250` (`_real_checkout`)
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write the failing wiring test**

Append (no git/rsync — all three step helpers are monkeypatched):

```python
# --- _real_checkout composition (host-free, never skipped) --------------------------


def test_real_checkout_calls_steps_in_order_with_right_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    order: list[str] = []
    seen: dict[str, object] = {}

    def _sync(kernel_src: str, ws: Path) -> None:
        order.append("sync")
        seen["sync"] = (kernel_src, ws)

    def _stage(config_ref: str, ws: Path) -> None:
        order.append("stage")
        seen["stage"] = (config_ref, ws)

    def _patch(patch_ref: str, ws: Path) -> None:
        order.append("patch")
        seen["patch"] = (patch_ref, ws)

    monkeypatch.setattr(build_module, "_sync_tree", _sync)
    monkeypatch.setattr(build_module, "_stage_config", _stage)
    monkeypatch.setattr(build_module, "_apply_patch", _patch)

    profile = BuildProfile.parse(
        {**_VALID_PROFILE, "config_ref": "/configs/c", "patch_ref": "/patches/p"}
    )
    assert isinstance(profile, ServerBuildProfile)
    build_module._real_checkout("/src/linux", profile, workspace)

    assert order == ["sync", "stage", "patch"]
    assert seen["sync"] == ("/src/linux", workspace)
    assert seen["stage"] == ("/configs/c", workspace)
    assert seen["patch"] == ("/patches/p", workspace)


def test_real_checkout_skips_patch_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(build_module, "_sync_tree", lambda *_: order.append("sync"))
    monkeypatch.setattr(build_module, "_stage_config", lambda *_: order.append("stage"))
    monkeypatch.setattr(build_module, "_apply_patch", lambda *_: order.append("patch"))

    profile = _profile()  # patch_ref is None
    build_module._real_checkout("/src/linux", profile, tmp_path / "ws")

    assert order == ["sync", "stage"]  # no patch step
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k real_checkout -q`
Expected: FAIL — the current `_real_checkout` raises `MISSING_DEPENDENCY` instead of composing the steps.

- [ ] **Step 3: Rewrite `_real_checkout`**

Replace the stub body (and its `# pragma: no cover - live_vm`) at `build.py:243-250`:

```python
def _real_checkout(kernel_src: str, profile: ServerBuildProfile, workspace: Path) -> None:
    """Materialize a warm per-Run workspace, stage the ``.config``, apply any patch.

    Steps run in order so the resetting rsync (4a) precedes config-staging (4b) and patch
    application (4c); see the spec/ADR-0053 for the failure contract each step enforces.
    """
    _sync_tree(kernel_src, workspace)
    _stage_config(profile.config_ref, workspace)
    if profile.patch_ref is not None:
        _apply_patch(profile.patch_ref, workspace)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k real_checkout -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Add the helpers to `__all__` and commit**

Add `"_apply_patch"`, `"_resolve_local_ref"`, `"_stage_config"`, `"_sync_tree"` is NOT required for `__all__` (leading underscore names are private); instead confirm the tests import them directly by name (they do). Skip `__all__` edits.

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): compose the real warm-tree checkout seam (closes the stub)"
```

---

## Task 6: Fill the `live_vm` real-make end-to-end test

**Files:**
- Modify: `tests/providers/local_libvirt/test_build.py:256-267` (the `NotImplementedError` stub)

- [ ] **Step 1: Replace the stub body**

Swap the `raise NotImplementedError(...)` for a real end-to-end assertion. It drives the real
seams (`_real_checkout` → `_real_run_make` → `_real_read_build_id`) with the existing
`_FakeStore` (no S3), then independently parses `readelf -n vmlinux` and asserts equality.
Inputs come from the `live_vm` runner's env; absent any, it skips (the established pattern):

```python
@pytest.mark.live_vm
def test_live_vm_real_make_build_id_matches_readelf() -> None:  # pragma: no cover - live_vm
    import os
    import re
    import shutil
    import subprocess as sp
    import tempfile

    from kdive.providers.local_libvirt import build as build_module

    src = os.environ.get("KDIVE_KERNEL_SRC")
    # The runner stages a .config that satisfies the kdump/debuginfo preflight and points
    # KDIVE_TEST_BUILD_CONFIG at it (a path or file:// URL). Absent it, there is nothing to build.
    config_ref = os.environ.get("KDIVE_TEST_BUILD_CONFIG")
    if not src or not config_ref or not shutil.which("readelf") or not shutil.which("rsync"):
        pytest.skip("KDIVE_KERNEL_SRC / KDIVE_TEST_BUILD_CONFIG / readelf / rsync unavailable")

    with tempfile.TemporaryDirectory() as tmp:
        store = _FakeStore()
        builder = LocalLibvirtBuild(
            tenant=_TENANT,
            workspace_root=Path(tmp),
            store_factory=lambda: store,
            checkout=lambda _run, profile, ws: build_module._real_checkout(src, profile, ws),
            read_config=build_module._real_read_config,
            run_make=build_module._real_run_make,
            read_kernel_image=lambda ws: (ws / "arch/x86/boot/bzImage").read_bytes(),
            read_vmlinux=lambda ws: (ws / "vmlinux").read_bytes(),
            read_build_id=build_module._real_read_build_id,
        )
        profile = BuildProfile.parse(
            {
                "schema_version": 1,
                "kernel_source_ref": f"file://{src}",
                "config_ref": config_ref,
                "patch_ref": None,
            }
        )
        assert isinstance(profile, ServerBuildProfile)
        out = builder.build(_RUN, profile)

        vmlinux = Path(tmp) / str(_RUN) / "vmlinux"
        notes = sp.run(
            ["readelf", "-n", str(vmlinux)], capture_output=True, text=True, check=True
        ).stdout
        match = re.search(r"Build ID:\s*([0-9a-f]+)", notes)
        assert match is not None, "readelf reported no GNU build-id"
        assert out.build_id == match.group(1)
```

- [ ] **Step 2: Verify the test collects and skips cleanly (no `live_vm` env in CI/dev)**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -k live_vm_real_make -q`
Expected: the `live_vm` marker is deselected by the default `-m "not live_vm"` config, OR the
test SKIPs. Either way: no failure, no error. Confirm it does not error on collection.

- [ ] **Step 3: Commit**

```bash
git add tests/providers/local_libvirt/test_build.py
git commit -m "test(build): implement the live_vm real-make build-id assertion"
```

---

## Task 7: Full guardrails green

- [ ] **Step 1: Format + lint**

Run: `just format` then `just lint`
Expected: clean (no ruff errors, formatting stable).

- [ ] **Step 2: Type-check (whole tree)**

Run: `just type`
Expected: `ty check` passes with zero diagnostics.

- [ ] **Step 3: Full non-gated test suite**

Run: `just test`
Expected: all pass; the new helper/composition tests run, the `live_vm` test is deselected.

- [ ] **Step 4: Confirm no `pragma: no cover` regressions / coverage of new code**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q`
Expected: PASS. The only `# pragma: no cover - live_vm` remaining in `build.py` are
`_real_read_config`, `_real_run_make`, `_real_read_build_id`, and the `live_vm` test —
`_real_checkout`/`_sync_tree`/`_stage_config`/`_apply_patch`/`_resolve_local_ref` are fully
covered by mocked unit tests (no pragma).

- [ ] **Step 5: Commit any formatting-only changes**

```bash
git add -A
git commit -m "chore(build): formatting/lint cleanup for the checkout seam" || echo "nothing to commit"
```

---

## Self-Review notes

- **Spec coverage:** §4a→Task 4; §4b→Tasks 1-2; §4c→Tasks 1,3; §5 wiring→Task 5; §6 redaction→`_redacted_tail` (Tasks 1,3,4); §7 taxonomy→error categories across Tasks 3-4; §8 tests→Tasks 1-6; §9 acceptance→Tasks 5 (patch contract) + 6 (`live_vm` end-to-end).
- **No new public API:** all helpers are underscore-private and imported by name in tests; `build()`/`from_env`/the profile/the store are untouched.
- **Type consistency:** helper signatures are stable across tasks — `_resolve_local_ref(ref: str, *, kind: str) -> Path`, `_stage_config(config_ref: str, workspace: Path)`, `_apply_patch(patch_ref: str, workspace: Path)`, `_sync_tree(kernel_src: str, workspace: Path)`, `_real_checkout(kernel_src: str, profile: ServerBuildProfile, workspace: Path)`.
