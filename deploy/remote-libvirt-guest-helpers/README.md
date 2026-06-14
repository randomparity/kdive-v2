# remote-libvirt in-guest helpers (operator-provided)

ADR-0082/0084/0085 specify that a remote-libvirt **base image carries operator-provided**
in-guest helpers that the worker invokes over the qemu-guest-agent with fixed argv (never a
shell string). kdive deliberately does **not** ship these — they are a trust boundary the
operator owns. This directory holds a **reference implementation** so an evaluator can produce
a working base image (the campaign found the staged images and the repo both lacked them — see
`docs/reports/mcp-coverage-campaign-2026-06-13.md` F7).

Status: `kdive-install-kernel` is implemented and **executes in-guest** (verified via
qemu-guest-agent `guest-exec`). Full install→boot has not yet been validated end-to-end in the
campaign environment (it also depends on the object-store reachability of F8). `kdive-capture-vmcore`
(ADR-0084) and `kdive-drgn` (ADR-0085) are not yet written.

## The contract (ADR-0082 §1)

`/usr/local/sbin/kdive-install-kernel` — the only program the Install/Boot plane allowlists.

- `install --url <presigned-get> --cmdline <cmdline> --method <method>` — curl the ADR-0081
  gzip bundle (`boot/vmlinuz` + `lib/modules/<ver>`), install it, and **add-or-replace one
  deterministic grub slot** ("kdive") whose kernel cmdline is `<cmdline>` verbatim. Does NOT
  change the boot selection. `--method kdump` also enables the kdump service. Idempotent
  (replace, not append). Exit non-zero on any failure.
- `boot-id` — print `/proc/sys/kernel/random/boot_id`.
- `boot` — select the "kdive" slot for the **next boot only** (grub one-shot) and trigger a
  **detached** reboot into it, atomically.

## Installing into a Fedora base image (gotchas the campaign hit)

```bash
virt-customize -a fedora-kdive-remote-base-43.qcow2 \
  --copy-in kdive-install-kernel:/usr/local/sbin/ \
  --run-command 'chown root:root /usr/local/sbin/kdive-install-kernel' \
  --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel' \
  --run-command 'restorecon -v /usr/local/sbin/kdive-install-kernel'
```

- **`chown root:root` + `restorecon` are required.** `--copy-in` preserves the source file's
  uid/gid and gives it a generic SELinux type; on an SELinux-enforcing guest `guest-exec` then
  fails with a misleading `No such file or directory` (ENOENT, not EACCES). chown + relabel fixes it.
- The base image needs `curl`, `tar`, `depmod`, `dracut`, `grubby`, `grub2-reboot` (standard on
  a non-minimal Fedora; verify on a minimal image).

## Object-store reachability (F8)

The `install` helper curls the bundle from a presigned URL the worker mints against
`KDIVE_S3_ENDPOINT_URL`. That URL must be reachable **from the guest** — `http://localhost:9000`
(the dev default) is the guest's own loopback. Set `KDIVE_S3_ENDPOINT_URL` to a control-plane
address routable from the remote guest network, and ensure the guest can reach it. The same
applies to the capture upload (presigned PUT).
