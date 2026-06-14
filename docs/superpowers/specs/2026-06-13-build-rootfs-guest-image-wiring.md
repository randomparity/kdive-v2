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
targets stderr (`src/kdive/log.py`), so no logging change is needed.

The printed path is the resolved absolute `--dest` (`Path(args.dest).resolve()` — the
same file the image was moved to), so the line is correct regardless of the caller's
cwd.

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
| `tests/mcp/core/test_main.py` | extend `run_build_rootfs` test to assert stdout is exactly the eval-safe export line and to cover a path needing shell quoting. |

## Testing

Unit (non-gated), driving `run_build_rootfs` with the resolver/plane faked at the
existing seam (`monkeypatch` of `build_provider_resolver`) and capturing stdout:

- **Happy path:** stdout is exactly `export KDIVE_GUEST_IMAGE=<dest>\n` and nothing
  else; the moved file exists at `--dest`.
- **Shell-quoting edge:** a `--dest` containing a space yields a single
  `shlex.quote`-escaped token (round-trips through `shlex.split` to the original
  absolute path) — so `eval` would export the correct value.
- **stdout/stderr split:** the digest/path summary is **not** on stdout (it stays a
  log record), so an `eval "$(...)"` capture is clean. (Asserted by capturing stdout
  and checking the digest string is absent from it.)
- The existing tests (subcommand parsing, repeated `--package`, move-not-copy) stay
  green unchanged.

The real build and the boot it enables remain proven only on the operator-run live
stack (`docs/runbooks/image-lifecycle.md`), behind the existing live markers — this
change does not move that boundary.
