# Remote in-guest helpers: reference implementations + build/runbook wiring

- **Issue:** #374
- **Date:** 2026-06-13
- **Type:** docs + reference-implementation
- **ADRs realized (not authored here):** [0082](../../adr/0082-remote-install-in-guest-kernel.md)
  (`kdive-install-kernel`), [0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md)
  (`kdive-capture-vmcore`), [0085](../../adr/0085-drgn-live-transport-generalization.md)
  (`kdive-drgn`). This change ships reference implementations of the helpers those ADRs require
  the operator to provide; it does not change any provider contract.

## Problem

ADR-0082/0084/0085 fix the remote-libvirt base image as carrying three operator-provided
in-guest helpers that the worker invokes over the qemu-guest-agent with **fixed argv** (never a
shell string):

| helper | path | invoked by |
|---|---|---|
| `kdive-install-kernel` | `/usr/local/sbin/kdive-install-kernel` | `lifecycle/install.py` (`_HELPER`) |
| `kdive-capture-vmcore` | `/usr/local/sbin/kdive-capture-vmcore` | `retrieve/common.py` (`HELPER`) |
| `kdive-drgn` | `/usr/local/sbin/kdive-drgn` | `debug/introspect.py` (`_DRGN_HELPER`) |

Only `kdive-install-kernel` has a reference implementation in the repo (under
`deploy/remote-libvirt-guest-helpers/`). `kdive-capture-vmcore` and `kdive-drgn` have none, the
base-image build recipe in the host-setup runbook installs only the guest-agent + tooling (no
helpers), and the README documents only the install helper. A from-repo operator therefore
cannot produce a bootable base image with all three helpers (campaign finding F7).

## Decision

Provide reference implementations of the two missing helpers next to the existing one, document
how the base-image build plane installs all three (chown root + restorecon — the SELinux/ENOENT
trap from `docs/solutions/2026-06-13-virt-customize-copyin-selinux-guest-exec-enoent.md`), wire
the install step into the host-setup runbook §5, and guard the deploy helpers under `just
lint-shell` so they cannot rot. Each helper contract-matches exactly what the provider invokes
(verified against the call sites below); we do not invent argv or output the provider does not
consume.

### `kdive-capture-vmcore` (ADR-0084 §2) — Retrieve plane

The provider (`retrieve/kdump_capture.py`, `retrieve/common.py`) invokes exactly two
subcommands with fixed argv:

- **`inspect`** (`[HELPER, "inspect"]`): print **one JSON object** to stdout with keys
  `present` (bool), `sha256` (base64 string), `size_bytes` (int), `build_id` (string),
  `dmesg_b64` (base64 string). The worker's `_parse_inspect` consumes exactly these five keys.
  - `sha256` is the **base64** SHA-256 of the raw core file (the value S3 signs into the
    presigned PUT, matching `file_sha256_b64` worker-side: `openssl dgst -binary -sha256 | base64`).
  - `dmesg_b64` is **byte-capped** in the guest (ADR-0084 §2.1: oversized dmesg must not blow the
    guest-agent reply ceiling); base64 of the (possibly truncated) `dmesg --read-clear`-free
    kernel log read from the core.
  - `present=false` when no kdump core exists in the guest's dump storage → worker maps to
    `READINESS_FAILURE`.
  - Newest core under `/var/crash/*/vmcore` (Fedora `kdump-utils` default `path`) wins;
    overridable by `KDIVE_VMCORE_PATH` for non-default kdump configs.
- **`upload --url <presigned-put> --header <k:v> …`** (`[HELPER, "upload", "--url", url,
  "--header", "k:v", …]`): `curl --upload-file` the same core to the presigned PUT with each
  `--header` passed through verbatim (the signed-checksum + metadata headers). Non-zero exit on
  any curl failure → worker maps to `INFRASTRUCTURE_FAILURE`.

`build_id` is read from the core's `VMCOREINFO`/`vmlinuz` build-id; on a guest where the live
build-id is the running kernel's, read it from `/sys/kernel/notes` (GNU build-id note) as the
reference source. The provider stores the core unconditionally and enforces the build-id→Run
match at postmortem (ADR-0084 §"Build-id match"), so the helper's `build_id` is advisory metadata
the worker records, not a gate — a best-effort read that never fails the inspect.

### `kdive-drgn` (ADR-0085 §5 / ADR-0079) — live drgn

The provider (`debug/introspect.py`) invokes exactly `[_DRGN_HELPER, <helper>]` where `<helper>`
is one of the fixed three `tasks` | `modules` | `sysinfo` (worker validates against
`_LIVE_HELPERS` before the agent round-trip; the real `GuestAgentExec` allowlists only the single
program). Each prints **one JSON object** to stdout that the worker passes straight to
`debug_common.introspect.assemble_report` as the matching section, so the shape must match the
worker-side `helper_tasks`/`helper_modules`/`helper_sysinfo` producers exactly:

- `tasks` → `{"tasks": [{pid,tgid,comm,state,kernel_stack:[…]}, …], "truncated": bool}` —
  D-state (uninterruptible/blocked) tasks only, bounded.
- `modules` → `{"modules": [{name,size,refcount,used_by:[…],state}, …], "decode_errors": int,
  "all_failed": bool}`.
- `sysinfo` → `{release,version,machine,nodename,boot_cmdline,cpus_online,mem_total_pages}`.

The helper runs drgn against the **live kernel** (`drgn -k`, i.e. `/proc/kcore` + the running
kernel's debuginfo) in-guest and emits the section JSON. A non-zero exit (drgn cannot attach,
e.g. no kernel debuginfo in the guest) → worker maps to `DEBUG_ATTACH_FAILURE`; undecodable
stdout → `INFRASTRUCTURE_FAILURE`. drgn is already in the base-image install set
(`drgn` in the §5 `--install` list), so the helper depends on a package the runbook installs.

These helpers are implemented as a small embedded Python (drgn ships a Python module) for the
drgn one and POSIX-ish `bash` for the capture one, matching the existing `kdive-install-kernel`
style (`#!/bin/bash`, `set -euo pipefail`).

### Build-plane install (all three helpers)

The base-image build plane is the operator's out-of-band `virt-builder`/`virt-customize` step
documented in the host-setup runbook §5 — kdive ships no build script for it (the disk image is
an operator trust boundary). Document a `virt-customize --copy-in` of the whole
`deploy/remote-libvirt-guest-helpers/` directory followed, **for each helper**, by
`chown root:root` + `chmod 0755` + `restorecon` (the README/solution-doc invariant: `--copy-in`
preserves the workstation uid/gid and a generic SELinux type, so guest-exec fails ENOENT without
the relabel). Document it once in the README (canonical) and reference it from runbook §5 so the
two do not drift.

### Guardrail

`just lint-shell` currently lints only `scripts/`. Extend it to also `shellcheck` + `shfmt -i 2
-d` the executable helpers under `deploy/remote-libvirt-guest-helpers/`, so a shipped reference
helper that fails the same shell-quality bar the rest of the repo holds is caught in CI. The
existing `kdive-install-kernel` already passes both; the two new helpers must too.

## Non-goals

- No change to any provider code, ADR, or migration — this is the operator-supplied side of an
  already-merged contract.
- No automated end-to-end test of the helpers in CI: they execute only in-guest under the
  `live_vm`/hardware gate (the runbook validate step + the campaign rerun are the live control).
  No shell unit harness is introduced for one-off in-guest scripts.
- Not retiring or re-homing `kdive-install-kernel`; it is verified against its call site and kept
  as-is.
