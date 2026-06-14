---
title: qemu-guest-agent guest-exec fails ENOENT on a present executable after virt-customize --copy-in
date: 2026-06-13
tags: [environment-quirk, selinux, libvirt, virt-customize, qemu-guest-agent, remote-libvirt]
components: [deploy/remote-libvirt-guest-helpers/kdive-install-kernel, src/kdive/providers/remote_libvirt/lifecycle/install.py]
---

## Problem

Driving the remote-libvirt install plane, `runs.install` failed and the worker log showed:

```
libvirt: QEMU Driver error : internal error: unable to execute QEMU agent command
'guest-exec': Failed to execute child process "/usr/local/sbin/kdive-install-kernel"
(No such file or directory)
```

The helper had just been injected into the base image with:

```
virt-customize -a fedora-kdive-remote-base-43.qcow2 \
  --copy-in kdive-install-kernel:/usr/local/sbin/ \
  --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel'
```

`guestfish --ro` confirmed the file WAS present and looked correct:

```
-rwxr-xr-x 1 1000 1000 3810 ... kdive-install-kernel    # executable
#!/bin/bash                                              # clean shebang, no CRLF
/bin/bash  ->  is-file: true                             # interpreter present
```

So every obvious hypothesis was wrong: the file exists, is executable, has a valid shebang,
and the interpreter exists. `ENOENT` ("No such file or directory") for a present, executable
script is the misdirection.

## Root cause

`virt-customize --copy-in` preserves the **source file's uid/gid** (here `1000:1000`, the
workstation user) and assigns it a **generic/default SELinux type**, not the executable type a
program under `/usr/local/sbin` needs. On an SELinux-**enforcing** Fedora guest, the
qemu-guest-agent (running as root) is denied `execute` on the mislabeled file. The kernel /
guest-agent surfaces that failure as `ENOENT` rather than `EACCES`, which is what derails the
diagnosis — you go looking for a missing file instead of a wrong label.

The `--run-command 'chmod 0755 ...'` only fixes the mode, not the ownership or the SELinux
context, so it does not help.

## Solution

After `--copy-in`, set ownership to root and **relabel** the file:

```
virt-customize -a <base>.qcow2 \
  --copy-in kdive-install-kernel:/usr/local/sbin/ \
  --run-command 'chown root:root /usr/local/sbin/kdive-install-kernel' \
  --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel' \
  --run-command 'restorecon -v /usr/local/sbin/kdive-install-kernel'
```

Verified by booting a throwaway domain from a fresh overlay of the relabeled base image and
running guest-exec exactly as kdive does:

```
virsh qemu-agent-command helper-test \
  '{"execute":"guest-exec","arguments":{"path":"/usr/local/sbin/kdive-install-kernel",
   "arg":["boot-id"],"capture-output":true}}'
=> {"return":{"pid":897}}      # success: the agent launched the helper
```

(Before the chown+restorecon, the same guest-exec returned the ENOENT failure above.)

## Prevention

Always pair `virt-customize --copy-in` of an executable with `chown root:root` **and**
`restorecon` run-commands — never just `chmod`. This is now documented in
`deploy/remote-libvirt-guest-helpers/README.md` and the campaign rerun runbook
(`docs/runbooks/mcp-coverage-campaign-rerun.md`). No automated guard is practical (the image
build is out-of-band on the operator's host), so the runbook note is the control. When a
guest-agent `guest-exec` reports `ENOENT` for a path you can prove exists, suspect the SELinux
label / ownership before the file path.
