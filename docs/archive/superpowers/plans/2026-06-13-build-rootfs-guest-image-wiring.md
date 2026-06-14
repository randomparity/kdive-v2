# build-rootfs KDIVE_GUEST_IMAGE wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m kdive build-rootfs` prints an eval-safe `export KDIVE_GUEST_IMAGE=<dest>` line on success so the produced rootfs is directly wireable into the local-libvirt live spine, and the live-spine skip messages name that wiring.

**Architecture:** stdout carries exactly one machine-readable wiring line (`shlex.quote`d resolved `--dest`); the human summary (path + `sha256:` digest) stays on the stderr logger, so `eval "$(python -m kdive build-rootfs ...)"` is safe. Reword the two duplicated live-spine preflight skip messages and the runbook. No build/plane/schema/gate change.

**Tech Stack:** Python 3.13, argparse CLI (`src/kdive/__main__.py`), `shlex`, `pathlib`, pytest, `uv`/`ruff`/`ty`/`just`.

**Spec:** `docs/superpowers/specs/2026-06-13-build-rootfs-guest-image-wiring.md`
**ADR:** `docs/adr/0106-build-rootfs-guest-image-wiring.md`

---

## File Structure

- `src/kdive/images/rootfs_command.py` — `run_build_rootfs`: resolve `--dest`, after the move print the eval-safe `export` line to stdout, reword the `_log.info` summary to log the resolved path and name `KDIVE_GUEST_IMAGE`.
- `tests/mcp/core/test_main.py` — add stdout-exactness, shlex round-trip, and empty-stdout-on-failure tests for `run_build_rootfs`.
- `tests/integration/conftest.py` — reword `live_vm_preflight` skip message via a shared constant.
- `tests/integration/test_live_stack.py` — reword `_spine_preflight` skip message (same constant/string).
- `docs/runbooks/image-lifecycle.md` — add the `eval` one-liner; keep the manual `export` as fallback.

Guardrails (run before each commit): `just lint`, `just type`, `just test`. Final gate: `just ci`.

---

### Task 1: `run_build_rootfs` prints the eval-safe wiring line

**Files:**
- Modify: `src/kdive/images/rootfs_command.py` (`run_build_rootfs`, currently ends at the `_log.info(...)` line)
- Test: `tests/mcp/core/test_main.py`

The existing test `test_run_build_rootfs_moves_plane_output_to_dest` already fakes `build_provider_resolver` with an inline `_FakePlane`/`_FakeResolver` and calls `run_build_rootfs`. Tasks 1-2 add three more tests that need the same plumbing, so first extract a module-level helper to avoid copying the 11-field `ProviderRuntime` construction four times.

- [ ] **Step 1a: Extract a shared resolver-faking helper**

Add to `tests/mcp/core/test_main.py` (after the imports; add `import shlex` to the top imports). The helper builds a fake resolver whose `resolve(...)` returns a `ProviderRuntime` with only `rootfs_build_plane` set, for any object that exposes `build(spec)`:

```python
def _resolver_with_plane(plane: object) -> object:
    """A fake resolver whose resolve() returns a ProviderRuntime carrying only `plane`."""

    class _FakeResolver:
        def resolve(self, kind: ResourceKind) -> ProviderRuntime:
            assert kind is ResourceKind.LOCAL_LIBVIRT
            unused = cast(Any, object())
            return ProviderRuntime(
                profile_policy=unused, provisioner=unused, builder=unused, installer=unused,
                booter=unused, connector=unused, controller=unused, retriever=unused,
                crash_postmortem=unused, vmcore_introspector=unused, live_introspector=unused,
                rootfs_build_plane=cast(Any, plane),
            )

    return _FakeResolver()
```

Optionally refactor the existing `test_run_build_rootfs_moves_plane_output_to_dest` to use this helper too (its inline `_FakeResolver` is the same shape) — keep its `_FakePlane` local since it asserts move-not-copy semantics. This is a pure refactor; the test must still pass unchanged in behavior.

- [ ] **Step 1b: Write the failing stdout-exactness test**

```python
def test_run_build_rootfs_prints_eval_safe_export_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`build-rootfs` prints exactly one eval-safe export line to stdout on success."""
    produced = tmp_path / "plane-workspace" / "fedora-kdive-ready-43.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FakePlane()),
    )
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    run_build_rootfs(args)

    out = capsys.readouterr().out
    assert out == f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest.resolve()))}\n", (
        "stdout is exactly the eval-safe wiring line and nothing else"
    )
    assert "sha256:abc" not in out, "the digest summary stays on stderr, never on stdout"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/mcp/core/test_main.py::test_run_build_rootfs_prints_eval_safe_export_line -v`
Expected: FAIL — stdout is empty (the command prints nothing today), so the equality assertion fails.

- [ ] **Step 3: Implement the stdout line + reworded summary**

In `src/kdive/images/rootfs_command.py`: add `import shlex` near the top imports (after `import logging`, keep import order ruff-clean). Change `run_build_rootfs` so the move targets the resolved path and the success branch prints the wiring line. Replace the tail of `run_build_rootfs`:

```python
    dest = Path(args.dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(output.qcow2_path), str(dest))
    dest.chmod(0o644)
    _log.info(
        "built rootfs %s digest=%s; set KDIVE_GUEST_IMAGE to this path", dest, output.digest
    )
    print(f"export KDIVE_GUEST_IMAGE={shlex.quote(str(dest))}")
```

(`Path.resolve()` makes the printed path absolute and canonical regardless of cwd; the `_log.info` summary logs that same resolved `dest`. `print` writes to stdout; the logger writes to stderr — see the spec's eval-safety invariant.)

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/mcp/core/test_main.py::test_run_build_rootfs_prints_eval_safe_export_line -v`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run pytest tests/mcp/core/test_main.py -q`
Expected: all green.

```bash
git add src/kdive/images/rootfs_command.py tests/mcp/core/test_main.py
git commit -m "feat(build-rootfs): print eval-safe KDIVE_GUEST_IMAGE export line

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: shlex round-trip + empty-stdout-on-failure tests

**Files:**
- Test: `tests/mcp/core/test_main.py`

These pin the falsifiable edge/failure criteria from the spec's Testing section. No production change — Task 1's implementation already satisfies them; if a test fails, fix the implementation, not the test.

- [ ] **Step 1: Write the shlex round-trip test**

```python
def test_run_build_rootfs_export_line_round_trips_a_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --dest with a space is a single shlex-quoted token that round-trips to the path."""
    produced = tmp_path / "plane-workspace" / "img.qcow2"
    produced.parent.mkdir(parents=True)
    produced.write_bytes(b"image-bytes")

    class _FakePlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            return RootfsBuildOutput(qcow2_path=produced, digest="sha256:abc", provenance={})

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FakePlane()),
    )
    dest = tmp_path / "with space" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    run_build_rootfs(args)

    out = capsys.readouterr().out.strip()
    assert out.startswith("export KDIVE_GUEST_IMAGE=")
    value = out[len("export KDIVE_GUEST_IMAGE=") :]
    assert shlex.split(value) == [str(dest.resolve())], "one token, round-trips to the path"
```

- [ ] **Step 2: Write the empty-stdout-on-failure test**

```python
def test_run_build_rootfs_writes_nothing_to_stdout_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing build raises and prints no export line, so eval exports nothing."""
    from kdive.domain.errors import CategorizedError, ErrorCategory

    class _FailingPlane:
        def build(self, spec: object) -> RootfsBuildOutput:
            del spec
            raise CategorizedError("build blew up", category=ErrorCategory.PROVISIONING_FAILURE)

    monkeypatch.setattr(
        "kdive.images.rootfs_command.build_provider_resolver",
        lambda: _resolver_with_plane(_FailingPlane()),
    )
    dest = tmp_path / "rootfs" / "out.qcow2"
    args = build_parser().parse_args(["build-rootfs", "--dest", str(dest)])
    with pytest.raises(CategorizedError):
        run_build_rootfs(args)
    assert capsys.readouterr().out == "", "no export line is printed when the build fails"
```

- [ ] **Step 3: Run both tests, verify they pass**

Run: `uv run pytest tests/mcp/core/test_main.py -k "round_trips or build_failure" -v`
Expected: PASS (Task 1's implementation already satisfies these — `print` runs only after a successful move; `Path.resolve()` quotes correctly).

- [ ] **Step 4: Guardrails + commit**

Run: `just lint && just type && uv run pytest tests/mcp/core/test_main.py -q`
Expected: all green.

```bash
git add tests/mcp/core/test_main.py
git commit -m "test(build-rootfs): pin shlex round-trip and empty-stdout-on-failure

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Reword the live-spine skip messages

**Files:**
- Modify: `tests/integration/conftest.py` (`live_vm_preflight`, the `pytest.skip(...)` for `_GUEST_IMAGE_ENV`)
- Modify: `tests/integration/test_live_stack.py` (`_spine_preflight`, the identical `pytest.skip(...)`)

Both files already define `_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"` and skip with the verbatim string `f"{_GUEST_IMAGE_ENV} unset or missing; run \`python -m kdive build-rootfs\`"`. This is a message-only change — it does **not** alter which conditions skip, so it cannot un-gate or widen the live markers.

- [ ] **Step 1: Reword the conftest message**

In `tests/integration/conftest.py`, replace the skip call inside `live_vm_preflight`:

```python
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or points at a missing file; build the local-libvirt "
            f"rootfs with `python -m kdive build-rootfs` and set {_GUEST_IMAGE_ENV} to its "
            "--dest path (see docs/runbooks/image-lifecycle.md)"
        )
```

- [ ] **Step 2: Reword the test_live_stack message identically**

In `tests/integration/test_live_stack.py`, replace the skip call inside `_spine_preflight`:

```python
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or points at a missing file; build the local-libvirt "
            f"rootfs with `python -m kdive build-rootfs` and set {_GUEST_IMAGE_ENV} to its "
            "--dest path (see docs/runbooks/image-lifecycle.md)"
        )
```

- [ ] **Step 3: Verify the reworded tests still collect (no syntax/quote break)**

The `live_stack`/`live_vm` markers are deselected by the normal gate (`just test` runs `-m "not live_vm and not live_stack"`), and `_spine_preflight`'s `pytest.skip` lives inside the test body — so running the file directly without Docker may error at fixture setup, not skip. Verify the message edits did not break Python by collecting only:

Run: `uv run pytest tests/integration/test_live_stack.py tests/integration/conftest.py --collect-only -q`
Expected: collection succeeds (the reworded f-strings parse; tests are listed). Do **not** assert SKIP here — that requires the live env.

Then confirm the normal gate still deselects them cleanly:

Run: `uv run pytest -m "not live_vm and not live_stack" tests/integration -q`
Expected: the `live_stack`-marked tests are deselected; remaining non-gated integration tests pass or skip on absent Docker.

- [ ] **Step 4: Guardrails + commit**

Run: `just lint && just type`
Expected: green.

```bash
git add tests/integration/conftest.py tests/integration/test_live_stack.py
git commit -m "test(live-spine): name KDIVE_GUEST_IMAGE wiring in skip messages

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Document the eval one-liner in the runbook

**Files:**
- Modify: `docs/runbooks/image-lifecycle.md`

Today step 1 (~line 53) shows the `python -m kdive build-rootfs --dest ... ` block, then "Record the printed `sha256:` digest …" (~line 60); step 2 (~line 74) shows the manual `export KDIVE_GUEST_IMAGE=...`. Add the `eval` one-liner as the recommended path; keep the manual export as fallback.

- [ ] **Step 1: Add the eval one-liner under step 1**

After the "Record the printed `sha256:` digest …" paragraph (around line 60-66), add:

```markdown
On success the command prints exactly one line to stdout — the wiring for the live
spine — while the digest/path summary goes to stderr (the logger). To export it in one
step, capture stdout with `eval`:

​```bash
eval "$(python -m kdive build-rootfs \
  --dest /var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2 \
  --name fedora-kdive-ready-43 --releasever 43)"
# KDIVE_GUEST_IMAGE is now exported to the --dest path
​```
```

(Remove the zero-width-space characters before the triple backticks — they are only here to keep this markdown fenced inside the plan.)

- [ ] **Step 2: Note the manual export is the printed line / fallback**

In step 2 (the `export KDIVE_GUEST_IMAGE=...` block, ~line 74), add a one-line note above the block:

```markdown
If you did not use the `eval` form above, set it by hand — this is exactly the line
`build-rootfs` prints on stdout:
```

- [ ] **Step 3: Verify the fenced code blocks balance**

This is a prose+bash-fence edit; no gate validates it (`check-mermaid` only inspects mermaid blocks, which are unchanged). Eyeball that every opening ```` ``` ```` you added has a matching close and the `eval` block is valid bash. Optionally confirm fence parity:

Run: `awk '/^```/{n++} END{exit n%2}' docs/runbooks/image-lifecycle.md && echo "fences balanced"`
Expected: prints `fences balanced` (even number of fence markers).

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/image-lifecycle.md
git commit -m "docs(runbook): add eval one-liner for KDIVE_GUEST_IMAGE wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full gate

- [ ] **Step 1: Run the full local CI gate**

Run: `just ci`
Expected: all green (lint, type, lock-check, lint-shell, lint-workflows, check-mermaid, docs-check, config-docs-check, config-guard, chart-version-check, test). Docker-gated db/integration tests skip cleanly; live markers stay gated.

- [ ] **Step 2: If anything is red, fix and re-run**

Fix the specific failure, re-run the affected guardrail, then `just ci` again. Do not proceed with a red gate.

---

## Self-Review notes

- **Spec coverage:** stdout wiring line (Task 1), reworded stderr summary (Task 1), shlex round-trip + failure path (Task 2), both skip messages (Task 3), runbook eval one-liner + manual fallback (Task 4), gate (Task 5). All spec sections covered.
- **No un-gating:** Task 3 changes message text only; the `if not image or not Path(image).exists()` skip condition is untouched, so live markers are not widened.
- **Type consistency:** `RootfsBuildOutput`, `ProviderRuntime`, `ResourceKind`, `CategorizedError`/`ErrorCategory`, `build_parser`, `run_build_rootfs` match their real signatures in the repo (verified against `src/kdive/images/rootfs_command.py`, `src/kdive/providers/runtime.py`, `tests/mcp/core/test_main.py`).
