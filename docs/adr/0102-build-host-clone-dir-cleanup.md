# ADR 0102 — Clean up the per-run build workspace after a terminal build

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-13
- **Deciders:** build-install area

## Context

Both build providers (`LocalLibvirtBuild`, `RemoteLibvirtBuild`) materialize a
per-run workspace at `<workspace_root>/<run_id>` through the shared
`BuildHostOrchestrator.build_workspace` — an rsync of the warm `KDIVE_KERNEL_SRC`
tree on the worker, or a git clone on an SSH/ephemeral build host
(ADR-0099/0100/0101). `RemoteLibvirtBuild` already reclaims the module-staging
tree via an injected `staging_cleanup` seam (`shutil.rmtree` locally,
`BuildTransport.cleanup` over a transport), but **the per-run workspace itself is
never removed** on either path. On a long-lived SSH build host every build leaves
a full kernel clone behind, so disk grows without bound (issue #358). The
worker-local path leaks the rsync destination the same way.

Two facts bound the fix. First, build warmth lives in the persistent
`KDIVE_KERNEL_SRC` rsync *source*, never in the per-run destination, so deleting
the per-run workspace reclaims disk without harming incremental builds. Second,
both removal mechanisms are already best-effort and non-raising
(`shutil.rmtree(..., ignore_errors=True)`; `ShellBuildTransport.cleanup` runs
`rm -rf` and only logs on failure), so a removal placed in a `finally` cannot mask
a build's success or its original error.

The existing `try/finally` in `RemoteLibvirtBuild.build` sits *below*
`build_workspace`, so a build that fails during checkout/config/`make` — a
terminal outcome that has already created the clone — would still leak it. The
cleanup must therefore wrap `build_workspace`, which means the workspace path has
to be known before that call.

## Decision

We will remove the per-run workspace after every terminal build outcome (success
or failure), and make the shared `BuildHostOrchestrator` own that removal since it
already owns workspace creation.

- `BuildHostOrchestrator` gains an injected `cleanup: Callable[[Path], None]` seam
  (default: `shutil.rmtree(workspace, ignore_errors=True)`), a `workspace_path(run_id)`
  helper that both `build_workspace` and callers use to derive
  `<workspace_root>/<run_id>`, and a `cleanup_workspace(workspace)` method.
- Each provider's `build()` derives the workspace path up front and wraps the whole
  build — `build_workspace` included — in `try/finally`, calling
  `cleanup_workspace` in the `finally`. In `RemoteLibvirtBuild` the existing
  staging cleanup nests inside this outer workspace cleanup.
- `over_transport` injects `cleanup=lambda ws: transport.cleanup(str(ws))` so a
  remote build's clone is removed on the build host through the same transport that
  created it; the worker-local default stays `rmtree`.

## Consequences

- Build hosts and the worker no longer accumulate per-run clone/rsync trees; disk
  is bounded by in-flight builds rather than lifetime build count.
- The orchestrator's contract grows by one seam and two methods; both providers
  gain an outer `try/finally`. Cleanup is best-effort: a removal failure is
  swallowed (worker) or logged (transport) and never changes the build result.
- One residual remains: a worker killed mid-build (SIGKILL, OOM, node loss) never
  runs its `finally`, so that one clone leaks until manual reclaim. This is the
  case a reconciler sweep would cover; it is left as a future follow-up rather than
  built now (see Alternatives).

## Alternatives considered

- **Reconciler sweep of stale clone dirs whose Run is terminal.** This is the only
  option that also reclaims clones from a worker that died mid-build. Rejected for
  now because it needs machinery the inline path does not: a directory-listing
  capability on `BuildTransport` (none exists today), a reconciler-held transport
  handle and `host_workspace_root` per build host, and a `dirname → run_id → Run
  state` mapping to decide what is safe to delete. That is a large surface for a
  `priority:low` chore whose common case (a build that completes or fails on a live
  worker) the inline cleanup already covers. It remains the natural home for the
  killed-worker residual if that leak ever matters.
- **Provider-owned workspace-cleanup seam, mirroring `staging_cleanup`.** Rejected
  in favor of orchestrator ownership: the workspace is created by the shared
  orchestrator, so centralizing its destruction there avoids duplicating the seam,
  its default, and the path derivation across both providers. `staging_cleanup`
  stays provider-owned because module staging is remote-only and has no
  orchestrator counterpart.
- **Leave as-is.** Rejected: unbounded disk growth on any persistent build host.
