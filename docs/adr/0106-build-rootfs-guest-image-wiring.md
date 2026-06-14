# ADR 0106 — `build-rootfs` emits its `KDIVE_GUEST_IMAGE` wiring on stdout

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0092](0092-image-rootfs-lifecycle.md)
  (the `RootfsBuildPlane` port and the `LocalLibvirtRootfsBuildPlane` that
  `build-rootfs` already drives — the plane, the command, and the
  `docs/runbooks/image-lifecycle.md` flow all exist and produce a real qcow2;
  this ADR does **not** change the build itself) and
  [ADR-0014](0014-structured-logging.md) (the structured logger writes to
  **stderr**, which is what makes a clean stdout wiring line possible).
- **Spec:** [`../superpowers/specs/2026-06-13-build-rootfs-guest-image-wiring.md`](../superpowers/specs/2026-06-13-build-rootfs-guest-image-wiring.md)
- **Issue:** [#370](https://github.com/randomparity/kdive/issues/370)

## Context

The local-libvirt live spine (`tests/integration/test_live_stack.py`, the booting
`live_stack` suite) resolves the guest rootfs it boots from the `KDIVE_GUEST_IMAGE`
environment variable, which must point at a readable qcow2 file
(`tests/integration/conftest.py:live_vm_preflight`). The image is produced by
`python -m kdive build-rootfs`, which drives `LocalLibvirtRootfsBuildPlane` and
moves the built qcow2 to `--dest`.

The MCP tool-coverage campaign (`docs/reports/mcp-coverage-campaign-2026-06-13.md`,
finding **F3**) classified the local lifecycle as **"GAP (fixture)"**: not a broken
plane, but a workflow seam. Two facts compose into the block:

1. `run_build_rootfs` records its result only via `_log.info(...)` — which goes to
   **stderr** at `LOG_LEVEL` (ADR-0014). It emits **nothing on stdout** and **no
   machine-readable handle** an operator can wire into `KDIVE_GUEST_IMAGE`. The
   operator must read `docs/runbooks/image-lifecycle.md` to learn both the env-var
   name and the `--dest` path, then hand-retype the `export` line. The command
   knows the exact path it just wrote and does not surface it.
2. The `live_vm_preflight` skip message says only
   `"{KDIVE_GUEST_IMAGE} unset or missing; run `python -m kdive build-rootfs`"`.
   It names the env var (good) but never tells the operator that the *output* of
   that build is what `KDIVE_GUEST_IMAGE` must point at — so even a successful
   build leaves the lifecycle "blocked" until the runbook is cross-referenced.

The build plane is real and tested. The residual capability gap is the operator
workflow: the command should make its product directly wireable.

## Decision

On a successful build, `run_build_rootfs` prints **exactly one line to stdout**:

```
export KDIVE_GUEST_IMAGE=<shlex-quoted absolute --dest path>
```

and nothing else to stdout. The line is `shlex.quote`-escaped so a path containing
spaces or shell metacharacters stays a single, correct shell token. Because the line
is the only thing on stdout and the structured logger writes the human summary to
**stderr**, the command composes cleanly with the shell:

```bash
eval "$(python -m kdive build-rootfs ...)"   # KDIVE_GUEST_IMAGE now exported
```

The human-readable summary — the destination path and the `sha256:` content digest
(the image identity; a rootfs image has no kernel `build_id`) — stays on the logger
(stderr), expanded to name `KDIVE_GUEST_IMAGE` so an operator reading the terminal
sees the wiring without piping into `eval`.

The `live_vm_preflight` skip message is reworded to state that `KDIVE_GUEST_IMAGE`
must point at the qcow2 that `build-rootfs` produces, pointing at the runbook flow:

```
KDIVE_GUEST_IMAGE unset or points at a missing file; build the local-libvirt rootfs
with `python -m kdive build-rootfs` and set KDIVE_GUEST_IMAGE to its --dest path
(see docs/runbooks/image-lifecycle.md).
```

No new CLI flag is added: the stdout wiring line is always emitted (there is no
mode where an operator does *not* want it), and the stderr summary already honors
`LOG_LEVEL` for quieting.

## Consequences

- The full local lifecycle (build → boot → debug → crash → capture) is no longer
  gated on an operator cross-referencing the runbook: `build-rootfs`'s own output is
  the wiring. `eval "$(...)"` is the documented one-liner.
- stdout becomes a **stable contract** for `build-rootfs`: exactly one
  `export KDIVE_GUEST_IMAGE=<path>` line on success, eval-safe. A regression that
  prints anything else to stdout breaks `eval`; a unit test pins stdout to that one
  line. This matches the existing convention that one-shot operator commands
  (`migrate`, `seed-demo`, `--version`) print their result to stdout.
- The summary's destination/digest stays on stderr, so capturing stdout for `eval`
  never swallows the digest the operator records as the image identity.
- The runbook (`docs/runbooks/image-lifecycle.md`) gains the `eval` one-liner; the
  manual `export KDIVE_GUEST_IMAGE=...` step it documents is retained as the
  copy-by-hand fallback (it is what the command now prints).
- No build behavior, plane, schema, provenance, or gate changes. The libguestfs/KVM
  real-build path stays exercised only on the operator-run live stack
  (`docs/runbooks/image-lifecycle.md`), never in CI; this change is unit-testable by
  driving `run_build_rootfs` with the resolver/plane faked at the existing seam and
  capturing stdout.

## Considered & rejected

- **A `--print-env` / `--quiet` flag to opt into the wiring line.** Rejected: the
  operator *always* wants the wiring — the whole point of the command is to produce a
  bootable image and tell you how to use it. A flag adds a speculative mode (YAGNI)
  and a way to land back in the F3 gap (build succeeds, no handle printed). The
  stderr summary already respects `LOG_LEVEL` for anyone who wants the terminal
  quiet; stdout stays the single-purpose wiring channel.
- **Write a sourceable env file (`<dest>.env`) next to the image.** Rejected: it
  adds a second artifact and a file-location convention the operator must *also*
  discover — it relocates the "where is the handle" problem rather than removing it.
  stdout is already the operator's terminal; printing the line there needs no new
  path to learn.
- **Print the `export` line to stderr alongside the summary.** Rejected: it would
  pollute the `eval "$(...)"` capture with the human summary and break shell
  composition. Splitting machine-readable wiring (stdout) from human/log output
  (stderr) is exactly what makes the one-liner safe.
- **Have `build-rootfs` set/persist `KDIVE_GUEST_IMAGE` itself (e.g. write to a
  profile or a dotenv the server reads).** Rejected: a child process cannot mutate
  its parent shell's environment, and silently writing to an operator's shell
  profile is surprising and non-portable. Emitting the line for the operator (or
  `eval`) to apply keeps the command side-effect-free outside its `--dest`.
