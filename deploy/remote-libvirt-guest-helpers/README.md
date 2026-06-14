# remote-libvirt in-guest helpers (operator-provided)

ADR-0082/0084/0085 specify that a remote-libvirt **base image carries operator-provided**
in-guest helpers that the worker invokes over the qemu-guest-agent with fixed argv (never a
shell string). kdive deliberately does **not** ship these in the running image — they are a trust
boundary the operator owns. This directory holds **reference implementations** so a from-repo
operator can produce a working base image with all required helpers (the MCP coverage campaign
found the staged images and the repo both lacked them — see
`docs/reports/mcp-coverage-campaign-2026-06-13.md` F7).

| helper | in-guest path | plane | ADR |
|---|---|---|---|
| `kdive-install-kernel` | `/usr/local/sbin/kdive-install-kernel` | Install/Boot | 0082 |
| `kdive-capture-vmcore` | `/usr/local/sbin/kdive-capture-vmcore` | Retrieve (vmcore) | 0084 |
| `kdive-drgn` | `/usr/local/sbin/kdive-drgn` | live drgn debug | 0085/0079 |

Each helper's argv and stdout contract is matched **exactly** to the program the provider
invokes (`lifecycle/install.py`, `retrieve/{common,kdump_capture}.py`, `debug/introspect.py`).
All three pass `shellcheck` + `shfmt -i 2 -d` and are guarded in CI by `just lint-shell`.

## Contracts

### `kdive-install-kernel` (ADR-0082 §1)

- `install --url <presigned-get> --cmdline <cmdline> --method <method>` — curl the ADR-0081
  gzip bundle (`boot/vmlinuz` + `lib/modules/<ver>`), install it, and **add-or-replace one
  deterministic grub slot** ("kdive") whose kernel cmdline is `<cmdline>` verbatim. Does NOT
  change the boot selection. `--method kdump` also enables the kdump service. Idempotent
  (replace, not append). Exit non-zero on any failure.
- `boot-id` — print `/proc/sys/kernel/random/boot_id`.
- `boot` — select the "kdive" slot for the **next boot only** (grub one-shot) and trigger a
  **detached** reboot into it, atomically.

### `kdive-capture-vmcore` (ADR-0084 §2)

- `inspect` — print **one JSON object** for the local kdump core:
  `{"present":bool,"sha256":"<base64>","size_bytes":int,"build_id":"<hex>","dmesg_b64":"<base64>"}`.
  `sha256` is the **base64** SHA-256 of the raw core (the value S3 signs into the presigned PUT).
  `dmesg_b64` is byte-capped in-guest (`KDIVE_DMESG_CAP_BYTES`, default 1 MiB) so an oversized
  ring buffer cannot exceed the guest-agent reply ceiling. `present=false` (no core) is a clean
  exit-0 reply the worker maps to `READINESS_FAILURE`. The core is the newest
  `/var/crash/*/vmcore` (kdump-utils default), overridable via `KDIVE_VMCORE_PATH`.
- `upload --url <presigned-put> --header <k:v> …` — `curl --upload-file` the same core to the
  presigned PUT, passing each `--header` through verbatim (the signed-checksum + metadata
  headers). Non-zero exit on any curl failure → `INFRASTRUCTURE_FAILURE`.

### `kdive-drgn` (ADR-0085 §5 / ADR-0079)

- `tasks` | `modules` | `sysinfo` — run drgn against the **live kernel** (`drgn -k`) and print
  **one JSON object** that the worker passes straight to
  `debug_common.introspect.assemble_report` as the matching section. The shapes mirror the
  worker-side `helper_tasks`/`helper_modules`/`helper_sysinfo` producers field-for-field
  (D-state tasks bounded at 200; module name/size/refcount/used_by/state; uts +
  boot_cmdline + online-cpu/total-page counters). A non-zero exit (drgn cannot attach — e.g. no
  kernel debuginfo in the guest) → `DEBUG_ATTACH_FAILURE`; undecodable stdout →
  `INFRASTRUCTURE_FAILURE`.

  The embedded drgn script is kept in sync with
  `src/kdive/providers/debug_common/{drgn_program,introspect}.py`; change them together.

## Installing into a Fedora base image (gotchas the campaign hit)

Copy the **whole directory** in, then for **each** helper set root ownership, mode, and SELinux
label — `--copy-in` preserves the source file's uid/gid and a generic SELinux type, so on an
SELinux-enforcing guest `guest-exec` then fails with a misleading `No such file or directory`
(ENOENT, not EACCES) until you chown + relabel. `chmod` alone does not fix it. See
`docs/solutions/2026-06-13-virt-customize-copyin-selinux-guest-exec-enoent.md`.

```bash
HELPERS="kdive-install-kernel kdive-capture-vmcore kdive-drgn"
args=(--copy-in deploy/remote-libvirt-guest-helpers/kdive-install-kernel:/usr/local/sbin/
  --copy-in deploy/remote-libvirt-guest-helpers/kdive-capture-vmcore:/usr/local/sbin/
  --copy-in deploy/remote-libvirt-guest-helpers/kdive-drgn:/usr/local/sbin/)
for h in $HELPERS; do
  args+=(--run-command "chown root:root /usr/local/sbin/$h"
    --run-command "chmod 0755 /usr/local/sbin/$h"
    --run-command "restorecon -v /usr/local/sbin/$h")
done
virt-customize -a fedora-kdive-remote-base-43.qcow2 "${args[@]}"
```

Base-image package prerequisites (all standard on a non-minimal Fedora — verify on a minimal
image):

- `kdive-install-kernel`: `curl`, `tar`, `depmod`, `dracut`, `grubby`, `grub2-reboot`.
- `kdive-capture-vmcore`: `curl`, `openssl`, `python3` (the build-id note reader), and a
  configured `kdump`/`kdump-utils` so a panic writes `/var/crash/*/vmcore`.
- `kdive-drgn`: `drgn` plus the **running kernel's debuginfo** in-guest (drgn needs DWARF to
  attach to the live kernel).

These map onto the host-setup runbook §5 `virt-builder --install` set
(`qemu-guest-agent,drgn,kexec-tools,makedumpfile,kdump-utils`); add `kernel-debuginfo` if you
intend to drive live drgn.

## Object-store reachability (F8)

The `install` helper curls the bundle from a presigned URL the worker mints against
`KDIVE_S3_ENDPOINT_URL`; the `upload` step of `kdive-capture-vmcore` PUTs to a presigned URL
minted the same way. Both URLs must be reachable **from the guest** — `http://localhost:9000`
(the dev default) is the guest's own loopback. Set `KDIVE_S3_ENDPOINT_URL` to a control-plane
address routable from the remote guest network, and ensure the guest can reach it.
