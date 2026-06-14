# remote-libvirt provider

The remote-libvirt provider drives QEMU/KVM guests on a separate target host over a
TLS-secured libvirt connection, so the worker and the guests run on different machines.

## What it needs

- **TLS PKI.** libvirt's TLS transport authenticates both ends with X.509 certificates. The
  target host serves a CA, server cert, and key; the worker presents a client cert the CA
  signed. The connection URI is the TLS form (for example `qemu+tls://HOST/system`).
- **virtproxyd.** The target runs the modular libvirt proxy daemon listening on the TLS
  port (16514) and forwards to the QEMU driver. The host firewall must permit that port
  from the worker.
- **Guest helpers.** Remote build, install, capture, and in-target artifact transfer use a
  guest agent and a small set of allowlisted in-guest helpers; the base guest image must
  ship them (and the tools they call, such as `tar`, with an SELinux policy that does not
  confine the agent).

All connection settings — the TLS URI, the gdbstub address, credentials — are in
[the config reference](../../guide/reference/config.md).

## Preflight

Check that the provider can reach a target before the first run:

```bash
just check-remote-libvirt HOST USER URI
```

The preflight reports reachability and TLS problems without changing either host.

## Host setup

The [remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md) covers
provisioning a target host end to end: the PKI, virtproxyd, the firewall ACL, and the guest
image with its helpers.
