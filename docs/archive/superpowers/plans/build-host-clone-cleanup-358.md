# Plan — Clean up the per-run build workspace after a terminal build (#358)

Design: [ADR-0102](../../adr/0102-build-host-clone-dir-cleanup.md). The per-run
workspace `<workspace_root>/<run_id>` (rsync dest on the worker, git clone on a
build host) is never removed; this plan adds best-effort removal after every
terminal build, owned by the shared `BuildHostOrchestrator`.

Tasks are tightly coupled (the orchestrator contract and both providers change
together), so they execute in one session, in order, each with a failing test
first. Guardrails before every commit: `just lint`, `just type`, and the focused
test files; `just test` before the final push.

Conventions: `ToolResponse`/`CategorizedError` are untouched (no tool-surface
change). Functions ≤100 lines, ≤8 complexity, absolute imports, Google-style
docstrings on new public methods. No new dependency, schema, or migration.

## Task 1 — Orchestrator owns workspace lifecycle

**Where it fits:** the foundation both providers call into.

**Files:** `src/kdive/providers/build_host/orchestration.py`,
`tests/providers/build_host/test_orchestration.py` (new).

**Change:**
- Add `type WorkspaceCleanup = Callable[[Path], None]`.
- Add a module-level `_default_workspace_cleanup(workspace: Path) -> None` that
  calls `shutil.rmtree(workspace, ignore_errors=True)`.
- Add a `cleanup: WorkspaceCleanup` field to `BuildHostOrchestrator`.
- `create(...)` gains `cleanup: WorkspaceCleanup | None = None`, defaulting to
  `_default_workspace_cleanup` when None.
- Add `workspace_path(self, run_id: UUID) -> Path` returning
  `self.workspace_root / str(run_id)`; have `build_workspace` call it instead of
  inlining the join (behavior identical).
- Add `cleanup_workspace(self, workspace: Path) -> None` that calls `self.cleanup`.

**TDD / acceptance:**
- Test `workspace_path` returns `<root>/<run_id>`.
- Test `cleanup_workspace` invokes the injected seam with the given path
  (recording seam).
- Test the default seam removes a real `tmp_path` directory and is a no-op on a
  non-existent path (no raise).
- `build_workspace` existing behavior unchanged (covered by provider tests).

**Rollback:** revert the file; no persisted state.

## Task 2 — `RemoteLibvirtBuild.build` reclaims the workspace

**Where it fits:** the remote provider already cleans staging; add the outer
workspace cleanup that also covers a `build_workspace` failure.

**Files:** `src/kdive/providers/remote_libvirt/build.py`,
`tests/providers/remote_libvirt/test_build.py`,
`tests/providers/remote_libvirt/test_build_transport.py`.

**Change:**
- `__init__` gains `workspace_cleanup: WorkspaceCleanup | None = None`, threaded
  into `BuildHostOrchestrator.create(cleanup=workspace_cleanup)`.
- `build()`: derive `workspace = self._orchestrator.workspace_path(run_id)` before
  the build; wrap the whole body (including `build_workspace`) in `try`, with
  `finally: self._orchestrator.cleanup_workspace(workspace)`. The existing
  `staging_cleanup` `finally` nests inside, so staging is removed first.
- `over_transport`: pass
  `workspace_cleanup=lambda ws: transport.cleanup(str(ws))`.

**TDD / acceptance:**
- Inject a recording `workspace_cleanup` into the test `_builder`; assert it is
  called once with `<ws_root>/<run_id>` on a successful build.
- Assert it is still called with that path when the build fails (e.g.
  `modules_install_returncode=1` and an `olddefconfig` failure inside
  `build_workspace`).
- Transport routing: drive a build through `over_transport` itself —
  `base.over_transport(fake_transport, host_workspace_root=<root>, git_remote=...,
  git_ref=..., secret_registry=...).build(run_id, profile)` — and assert the fake
  transport recorded a `cleanup` call for `<host_workspace_root>/<run_id>`. A
  directly-constructed transport builder does **not** exercise this: it keeps the
  default `rmtree` seam, so only `over_transport` proves the SSH/ephemeral
  build-host wiring (the issue's primary disk-leak case).
- Existing staging-cleanup and happy-path tests stay green.

**Rollback:** revert the file; default arg keeps prior construction call sites
valid.

## Task 3 — `LocalLibvirtBuild.build` reclaims the workspace

**Where it fits:** the local provider has no staging cleanup today; it gets the
same outer workspace cleanup.

**Files:** `src/kdive/providers/local_libvirt/build.py`,
`tests/providers/local_libvirt/test_build.py`.

**Change:** identical shape to Task 2 — `workspace_cleanup` param threaded to the
orchestrator, `build()` derives the path and wraps the body in `try/finally`
calling `cleanup_workspace`, `over_transport` injects the transport-routed
cleanup.

**TDD / acceptance:**
- Recording-seam test: cleanup called once with `<ws_root>/<run_id>` on success.
- Failure-path test: cleanup still called on a build failure.
- Transport routing: drive the build through `over_transport` (not a
  directly-constructed builder, which keeps the default `rmtree`) and assert the
  fake transport recorded a `cleanup` call for `<host_workspace_root>/<run_id>`.
- Existing happy-path and failure tests stay green.

**Rollback:** revert the file.

## Verification (whole change)

- `just lint`, `just type`, then `just test` green.
- The `live_vm`-gated real-make tests stay gated (untouched).
- Adversarial branch review (`/challenge --base main`) returns approve.
