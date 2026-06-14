# `build-rootfs` emits its `KDIVE_GUEST_IMAGE` wiring — design

- **Date:** 2026-06-13
- **Issue:** [#370](https://github.com/randomparity/kdive/issues/370)
- **ADR:** [ADR-0106](../../adr/0106-build-rootfs-guest-image-wiring.md)
- **Status:** Proposed

## Problem

The local-libvirt live spine boots the guest rootfs named by the `KDIVE_GUEST_IMAGE`
environment variable, which must point at a readable qcow2. That qcow2 is produced by
`python -m kdive build-rootfs`, which already drives the real
`LocalLibvirtRootfsBuildPlane` (virt-builder → whole-disk ext4 repack → guestfish
normalization → content-digest), records provenance, and moves the result to `--dest`.

The MCP tool-coverage campaign (`docs/reports/mcp-coverage-campaign-2026-06-13.md`,
finding **F3**) classified the local lifecycle as **"GAP (fixture)"** — the plane is
not a stub; the *workflow* is the gap. `run_build_rootfs` records its result only via
`_log.info(...)`, which goes to **stderr** at `LOG_LEVEL` (ADR-0014), and prints
**nothing to stdout** and **no machine-readable handle**. The operator must read
`docs/runbooks/image-lifecycle.md` to learn both the env-var name and the path, then
hand-retype the `export` line — even though the command knows the exact path it just
wrote. The live preflight's skip message names the env var but never says the build's
*output* is what it must point at.

The acceptance criterion for #370 — "`python -m kdive build-rootfs` produces a usable
guest rootfs image that the local-libvirt spine can boot, so the full local lifecycle
is no longer blocked on a stub" — is met by the existing plane *plus* closing this
last workflow seam so the produced image is directly wireable.

## Goals

- `build-rootfs` makes its product directly usable: on success it emits the exact
  `KDIVE_GUEST_IMAGE` wiring with no runbook cross-reference.
- The output composes with the shell: `eval "$(python -m kdive build-rootfs ...)"`
  exports `KDIVE_GUEST_IMAGE` correctly, including for paths with spaces.
- The human-readable summary (path + `sha256:` digest) remains available on the
  terminal and is not swallowed by the `eval` capture.
- The live-spine skip message tells a post-build operator the wiring.

## Non-goals

- **No change to the build itself** — plane, stages, provenance, digest, `--dest`
  move, permissions are all untouched.
- **No new gate behavior.** The real libguestfs/KVM build stays exercised only on the
  operator-run live stack (the `image-lifecycle` runbook), never in CI. The live
  `live_stack`/`live_vm` markers stay gated and are not widened.
- **No reproducible-rebuild work** (an explicit non-goal of ADR-0092, unchanged).
- The command does **not** persist or mutate any shell environment — it prints; the
  operator (or `eval`) applies.

## Design

### stdout = the wiring contract; stderr = human/log

On a successful build, `run_build_rootfs` prints **exactly one line to stdout**:

```
export KDIVE_GUEST_IMAGE=<shlex.quote(absolute --dest path)>
```

`shlex.quote` makes a path with spaces or shell metacharacters a single correct shell
token. Nothing else is written to stdout. The structured logger (stderr, ADR-0014)
carries the human summary — the destination path and the `sha256:` content digest —
reworded to name `KDIVE_GUEST_IMAGE` so a human reading the terminal sees the wiring
without piping into `eval`.

Because stdout holds only the one line and the summary is on stderr, the shell
one-liner is safe:

```bash
eval "$(python -m kdive build-rootfs ...)"   # KDIVE_GUEST_IMAGE now exported
```

This mirrors the existing convention that one-shot operator commands (`migrate`,
`seed-demo`, `--version`) print their result to stdout; the structured logger already
targets stderr, so no logging change is needed.

**Eval-safety invariant.** `eval "$(python -m kdive build-rootfs ...)"` is safe only
if the wiring line is the *only* thing on stdout — i.e. every log record the command
emits goes to stderr. This holds for the whole command, not just the build summary:
the stdlib handler is `_KdiveHandler(sys.stderr)` (`src/kdive/log.py`), and the
ADR-0090 "stdout floor" installed by `bootstrap_stdout_floor` delegates to that same
stderr configurator (the name is historical; the stream is stderr). So the always-on
`main()` startup line (`"starting kdive ..."`) and the build summary both land on
stderr, leaving stdout to the single `export` line. The unit test pins this by
asserting stdout *equals* the one line exactly (any stray stdout write fails it).

The printed path is the resolved absolute `--dest` (`Path(args.dest).resolve()` — the
same file the image was moved to). The reworded `_log.info` summary logs that same
resolved path, so the path the operator sees on stderr and the one in the `export`
line are identical. The line is correct regardless of the caller's cwd.

### Failure mode

On a build failure the plane/command raises a `CategorizedError`, which propagates out
of `run_build_rootfs` to `main()` and exits non-zero **before** any `export` line is
printed — nothing is written to stdout. So `eval "$(python -m kdive build-rootfs ...)"`
of a failed build exports nothing (it does not leave `KDIVE_GUEST_IMAGE` pointing at a
half-written or stale image), and the non-zero exit is observable to the operator and
to scripts.

### Live-spine skip message

Both copies of the spine preflight skip message — `tests/integration/conftest.py`
(`live_vm_preflight`) and `tests/integration/test_live_stack.py` (`_spine_preflight`)
— are reworded from

> `KDIVE_GUEST_IMAGE unset or missing; run `python -m kdive build-rootfs``

to a message that names the wiring and the runbook:

> `KDIVE_GUEST_IMAGE unset or points at a missing file; build the local-libvirt rootfs
> with `python -m kdive build-rootfs` and set KDIVE_GUEST_IMAGE to its --dest path
> (see docs/runbooks/image-lifecycle.md).`

A small shared constant keeps the two sites identical (they already duplicate the
string verbatim today); the rewording does not change which conditions skip.

### Runbook

`docs/runbooks/image-lifecycle.md` gains the `eval "$(python -m kdive build-rootfs ...)"`
one-liner as the recommended path and keeps the explicit `export KDIVE_GUEST_IMAGE=...`
step as the copy-by-hand fallback (which is exactly what the command now prints).

## Components touched

| unit | change |
|------|--------|
| `src/kdive/images/rootfs_command.py` | `run_build_rootfs` prints the eval-safe `export` line to stdout; the `_log.info` summary is reworded to name `KDIVE_GUEST_IMAGE`. |
| `tests/integration/conftest.py` | reword the `live_vm_preflight` skip message. |
| `tests/integration/test_live_stack.py` | reword the `_spine_preflight` skip message (identical string). |
| `docs/runbooks/image-lifecycle.md` | add the `eval` one-liner; keep the manual export as fallback. |
| `tests/mcp/core/test_main.py` | extend the `run_build_rootfs` tests: stdout equals exactly the eval-safe export line (resolved path); a space-bearing `--dest` round-trips through `shlex.split`; a `build()` that raises writes nothing to stdout. |

## Testing

Unit (non-gated), driving `run_build_rootfs` with the resolver/plane faked at the
existing seam (`monkeypatch` of `build_provider_resolver`) and capturing stdout:

- **Happy path:** stdout is exactly `export KDIVE_GUEST_IMAGE=<resolved dest>\n` and
  nothing else; the moved file exists at `--dest`. The exact-equality assertion is the
  falsifiable signal — it subsumes "digest/summary absent from stdout" (any stray
  stdout write, including a regressed digest print, fails the equality), so no separate
  weaker substring check is needed.
- **Shell-quoting edge:** a `--dest` containing a space yields a single
  `shlex.quote`-escaped token; `shlex.split` of the printed line's value round-trips to
  the resolved absolute path — so `eval` would export the correct single value.
- **Failure path:** when the faked plane's `build()` raises a `CategorizedError`,
  `run_build_rootfs` propagates it and writes **nothing** to stdout (asserted: captured
  stdout is empty), so an `eval` capture of a failed build exports nothing.
- The existing tests (subcommand parsing, repeated `--package`, move-not-copy) stay
  green unchanged.

The real build and the boot it enables remain proven only on the operator-run live
stack (`docs/runbooks/image-lifecycle.md`), behind the existing live markers — this
change does not move that boundary.
