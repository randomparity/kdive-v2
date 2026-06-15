# Runbook: standing up a remote-libvirt host and registering it

Operator guide for taking a bare Linux box to a kdive **remote-libvirt** provider host: install
the virtualization stack, configure `qemu+tls` mutual TLS, build the operator-staged base image,
and register the host with a running kdive deployment. It complements
[remote-live-stack.md](remote-live-stack.md) (which assumes the host is already configured) by
covering the host bring-up itself.

The commands below were exercised on **Ubuntu 24.04** (`ub24-big`, 64 vCPU / 251 GiB, KVM,
passwordless sudo) against a kdive **Helm demo** release in namespace `kdive-demo`. Substitute your
own host FQDN/IP, pool, and cluster network where noted. The Ubuntu-24.04-specific items in
[step 4](#4-libguestfs-prerequisites-for-the-image-build) are the non-obvious part; on other
distros the libguestfs appliance may already have what it needs.

## What this produces

- A host exporting `qemu+tls://<host>/system` with x509 mutual TLS (`no_verify` forbidden).
- A storage pool holding the operator-staged base image
  (`fedora-kdive-remote-base-43.qcow2`, the `RemoteLibvirtRootfsBuildPlane` artifact).
- A gdbstub-port ACL restricting the debug ports to the worker pool's source.
- A kdive deployment whose worker resolves the TLS secret refs and connects to the host.

## Prerequisites

- A host with `/dev/kvm` (`virt-host-validate qemu` should pass hardware-virtualization checks)
  and passwordless sudo.
- Network reachability **both ways** between the kdive worker and the host: the worker reaches
  the host's `16514` (TLS) and the gdbstub port range; the guest reaches the object store
  (see [step 7](#7-register-remote-libvirt-on-the-deployment) and the MinIO note).
- For the image build: outbound HTTPS from the host (virt-builder fetches the Fedora template
  and dnf metadata).

## 1. Install the virtualization stack

```bash
sudo apt-get update
sudo apt-get install -y \
  libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils \
  libguestfs-tools virtinst gnutls-bin \
  pkg-config libvirt-dev build-essential python3-dev   # only if building images on this host
sudo systemctl enable --now libvirtd
sudo virt-host-validate qemu | grep -i kvm             # expect PASS on hardware virt + /dev/kvm
```

Add the build user to the `kvm` and `libvirt` groups (effective on next login):

```bash
sudo usermod -aG kvm,libvirt "$USER"
```

## 2. Mutual-TLS PKI and the `qemu+tls` listener

Generate a CA, a libvirtd **server** cert (CN/SAN = the host FQDN **and** its IP), and a **client**
cert for the worker. `certtool` ships with `gnutls-bin`.

```bash
WORK=$(mktemp -d)
cd "$WORK"

# CA
certtool --generate-privkey > cakey.pem
printf 'cn = kdive remote-libvirt CA\nca\ncert_signing_key\n' > ca.info
certtool --generate-self-signed --load-privkey cakey.pem --template ca.info --outfile cacert.pem

# server cert — CN/SAN must cover how the worker addresses the host
certtool --generate-privkey > serverkey.pem
printf 'organization = kdive\ncn = HOST.FQDN\ndns_name = HOST.FQDN\nip_address = HOST.IP\ntls_www_server\nencryption_key\nsigning_key\n' > server.info
certtool --generate-certificate --load-privkey serverkey.pem \
  --load-ca-certificate cacert.pem --load-ca-privkey cakey.pem \
  --template server.info --outfile servercert.pem

# client cert — the worker identity
certtool --generate-privkey > clientkey.pem
printf 'organization = kdive\ncn = kdive-worker\ntls_www_client\nencryption_key\nsigning_key\n' > client.info
certtool --generate-certificate --load-privkey clientkey.pem \
  --load-ca-certificate cacert.pem --load-ca-privkey cakey.pem \
  --template client.info --outfile clientcert.pem
```

Install the server PKI and turn on the TLS listener:

```bash
sudo install -m0755 -d /etc/pki/CA /etc/pki/libvirt/private
sudo install -m0644 cacert.pem    /etc/pki/CA/cacert.pem
sudo install -m0644 servercert.pem /etc/pki/libvirt/servercert.pem
sudo install -m0600 serverkey.pem  /etc/pki/libvirt/private/serverkey.pem

sudo sed -i 's/^#*listen_tls = .*/listen_tls = 1/; s/^#*listen_tcp = .*/listen_tcp = 0/; s/^#*auth_tls = .*/auth_tls = "none"/' /etc/libvirt/libvirtd.conf
sudo systemctl stop libvirtd.service
sudo systemctl enable --now libvirtd-tls.socket     # socket activation owns the listener on 24.04
sudo systemctl start libvirtd.service
sudo ss -ltn | grep :16514                          # expect a LISTEN
```

`auth_tls = "none"` means cert-based mutual TLS with no extra SASL; verification stays on
(`no_verify` is forbidden by the provider's URI validation). Keep the **client** materials
(`cacert.pem`, `clientcert.pem`, `clientkey.pem`) for [step 7](#7-register-remote-libvirt-on-the-deployment).

On a host whose only role is throwaway debug guests, disable the libvirt AppArmor security
driver so qemu can read the pool's base image (its per-VM profile otherwise denies the overlay's
backing file, surfacing at provision as `Could not open '…/<base>.qcow2': Permission denied`):

```bash
sudo sed -i 's/^#*security_driver = .*/security_driver = "none"/' /etc/libvirt/qemu.conf
sudo systemctl restart libvirtd
```

Verify a mutual-TLS connection (client materials in `/etc/pki/libvirt` for this local check):

```bash
sudo install -m0644 clientcert.pem /etc/pki/libvirt/clientcert.pem
sudo install -m0600 clientkey.pem  /etc/pki/libvirt/private/clientkey.pem
sudo virsh -c "qemu+tls://HOST.FQDN/system" list --all
```

## 3. Storage pool and network

```bash
sudo virsh pool-define-as default dir --target /var/lib/libvirt/images
sudo virsh pool-build default && sudo virsh pool-start default && sudo virsh pool-autostart default
sudo virsh net-start default 2>/dev/null; sudo virsh net-autostart default
```

## 4. libguestfs prerequisites for the image build

The base image is built with `virt-builder --install`, which runs dnf **inside the libguestfs
appliance** — so the appliance needs a working network. On a default Ubuntu 24.04 host this fails
in four non-obvious ways; fix them once:

```bash
# (a) the appliance must read the host kernel
sudo chmod 0644 /boot/vmlinuz-*

# (b) passt (appliance user-net) self-sandboxes via an unprivileged userns; TWO layers block it.
#     (b1) the global unprivileged-userns restriction:
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/60-kdive-userns.conf
sudo sysctl --system
#     (b2) Ubuntu 24.04 also ships a per-binary AppArmor profile (usr.bin.passt) that confines
#     passt independently of (b1). If `passt exited with status 1` persists after (b1), unload it
#     (this is the actual blocker on a stock 24.04 host; (b1) alone is not enough):
sudo apparmor_parser -R /etc/apparmor.d/usr.bin.passt 2>/dev/null || true
sudo ln -sf /etc/apparmor.d/usr.bin.passt /etc/apparmor.d/disable/usr.bin.passt

# (c) THE key fix: the appliance gets no IP without a DHCP client on the host.
#     Install one and drop the stale supermin cache so it rebuilds with dhclient.
sudo apt-get install -y isc-dhcp-client
find /var/tmp/.guestfs-"$(id -u)" -mindepth 0 -delete 2>/dev/null || true
find "$HOME/.cache/libguestfs" -mindepth 0 -delete 2>/dev/null || true
```

Confirm the appliance now has network and DNS:

```bash
guestfish --network <<'GF'
add-drive-scratch 256M
run
debug sh "ip -o -4 addr show eth0; ip route; cat /etc/resolv.conf"
GF
# expect an address (e.g. 169.254.2.15), a default route, and a nameserver
```

Symptom map (what each missing item looks like): `(a)` → libguestfs cannot read the kernel; `(b)`
→ `passt exited with status 1` / `Failed to sandbox process` (on a stock 24.04 host this is the
`usr.bin.passt` AppArmor profile of `(b2)`, not the global sysctl of `(b1)`); `(c)` → eth0 stays
`DOWN`, dnf reports `Could not resolve host mirrors.fedoraproject.org`. `(b)` can alternatively be
sidestepped entirely by removing passt so libguestfs falls back to qemu slirp, but `(c)` is
required either way.

## 5. Build the operator-staged base image

The remote provider reaches the guest over **qemu-guest-agent** and boots a **bootable disk
image** (ADR-0078/0079/0080) — this is **not** the `python -m kdive build-fs` local-libvirt
artifact (which is a serial-readiness, whole-disk-ext4 image for direct-kernel boot). Build the
remote base image with the `RemoteLibvirtRootfsBuildPlane` recipe; the volume name must be
`fedora-kdive-remote-base-43.qcow2` (the provisioning profile derives it from
`REMOTE_BASE_IMAGE_NAME`).

Build (and customize, step 5a) against a **writable working copy**, then stage it into the pool —
the default pool dir `/var/lib/libvirt/images` is `root`-owned (`drwx--x--x`), so a non-root
`virt-builder`/`virt-customize` cannot write there directly. `virt-builder` also needs `/dev/kvm`
(`sudo chmod 0666 /dev/kvm` on a throwaway host, or finish the `kvm`-group login from step 1) or it
falls back to slow software emulation.

```bash
virt-builder fedora-43 --format qcow2 --size 10G \
  --output "$HOME/fedora-kdive-remote-base-43.qcow2" \
  --install qemu-guest-agent,drgn,kexec-tools,makedumpfile,kdump-utils,curl,tar,openssl,python3 \
  --run-command "systemctl enable qemu-guest-agent.service" \
  --run-command "systemctl enable kdump.service" \
  --run-command "sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config"
# (install the guest helpers into this copy next, step 5a, then stage it into the pool)
```

The matching `vmlinux`/debuginfo and a crashkernel-capable kernel remain the operator's content
contract (recorded in the plane's provenance); add them per your kdump needs. Add
`kernel-debuginfo` to the `--install` set if you intend to drive **live drgn** (`kdive-drgn`
needs the running kernel's DWARF to attach).

**SELinux must not confine the guest agent.** The Install/Retrieve/drgn helpers run **privileged
system mutations via `guest-exec`** (the install helper writes `/boot` + `/lib/modules` and runs
`depmod`/`dracut`/`grubby`). Fedora's targeted policy confines the agent to `virt_qemu_ga_t`,
which **cannot even read `/lib/modules`** — so an enforcing base image fails `runs.install` at the
helper's privileged `/boot` + `/lib/modules` mutation steps right after the bundle is extracted
(surfaced as a non-zero in-guest exit, now visible via the `install_failure` transcript, #386).
The `SELINUX=permissive` line above is the simplest fix for a test base image and is the form
verified end-to-end. To keep the rest of the guest enforcing you can instead try making **only**
the agent domain permissive (needs `policycoreutils-python-utils`):
`--run-command "semanage permissive -a virt_qemu_ga_t"` — but verify it on your image first: the
helper's `dracut`/`grubby`/`depmod` children may transition to other SELinux domains that a
per-domain permissive does not cover.

### 5a. Install the in-guest helpers (REQUIRED — install/capture/debug fail without them)

The Install, Retrieve, and live-drgn planes drive three operator-provided helpers over the
guest agent: `/usr/local/sbin/kdive-{install-kernel,capture-vmcore,drgn}` (ADR-0082/0084/0085).
kdive ships **reference implementations** under `deploy/remote-libvirt-guest-helpers/`; copy them
into the base image. **For each helper you must `chown root:root` + `restorecon`, not just
`chmod`** — `--copy-in` preserves the workstation uid/gid and a generic SELinux type, so on an
SELinux-enforcing guest `guest-exec` later fails with a misleading `No such file or directory`
(ENOENT, not EACCES). See
[`docs/archive/solutions/2026-06-13-virt-customize-copyin-selinux-guest-exec-enoent.md`](../../archive/solutions/2026-06-13-virt-customize-copyin-selinux-guest-exec-enoent.md).

```bash
HELPERS="kdive-install-kernel kdive-capture-vmcore kdive-drgn"
args=()
for h in $HELPERS; do
  args+=(--copy-in "deploy/remote-libvirt-guest-helpers/$h:/usr/local/sbin/"
    --run-command "chown root:root /usr/local/sbin/$h"
    --run-command "chmod 0755 /usr/local/sbin/$h"
    --run-command "restorecon -v /usr/local/sbin/$h")
done
virt-customize -a "$HOME/fedora-kdive-remote-base-43.qcow2" "${args[@]}"
```

Now stage the finished image into the (root-owned) storage pool and record its identity:

```bash
sudo install -m0644 -o root -g root \
  "$HOME/fedora-kdive-remote-base-43.qcow2" \
  /var/lib/libvirt/images/fedora-kdive-remote-base-43.qcow2
sudo virsh pool-refresh default
sudo virsh vol-list default
sudo sha256sum /var/lib/libvirt/images/fedora-kdive-remote-base-43.qcow2   # the image identity
```

The canonical install recipe, the exact argv/JSON contract each helper satisfies, and the
package prerequisites live in
[`deploy/remote-libvirt-guest-helpers/README.md`](../../../deploy/remote-libvirt-guest-helpers/README.md).

## 6. gdbstub-port ACL

The gdb-MI tier connects directly over TCP from the worker to the host's QEMU gdbstub port
(`qemu+tls` does not tunnel it), so the ACL is the auth. Restrict the TLS port and the gdbstub
range to the worker pool's source (here the cluster node network), leaving SSH untouched:

```bash
WORKER_CIDR=192.168.16.0/24            # the source the worker egresses from
sudo iptables -A INPUT -p tcp --dport 47000:47099 -s "$WORKER_CIDR" -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 47000:47099 -j DROP
sudo iptables -A INPUT -p tcp --dport 16514       -s "$WORKER_CIDR" -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 16514       -j DROP
```

Persist the rules with your firewall manager. The gdbstub range and the ACL'd listen address come
from the instance's `gdbstub_range` (e.g. `47000:47099`) and `gdb_addr` fields (see step 7);
`gdb_addr` has no default and provisioning fails closed without it.

## 7. Register remote-libvirt on the deployment

The provider is opt-in: it registers only when a `[[remote_libvirt]]` instance is declared in the
`systems.toml` inventory (ADR-0112), reconciled into the catalog (`KDIVE_SYSTEMS_TOML`; a mounted
ConfigMap in k8s). The TLS materials are **secrets-by-reference** — the inventory carries the refs
(filenames), and the worker resolves them through the file-ref backend under `KDIVE_SECRETS_ROOT`
and materializes a per-op `pkipath`. The Helm chart projects a Kubernetes Secret as files and sets
`KDIVE_SECRETS_ROOT` for you.

```bash
# the client materials from step 2
kubectl -n kdive-demo create secret generic kdive-remote-tls \
  --from-file=clientcert.pem --from-file=clientkey.pem --from-file=cacert.pem
```

The Secret **keys** and the `*_ref` values in the `systems.toml` block below must be the **same
filenames** — the backend resolves each ref by its literal name under `KDIVE_SECRETS_ROOT`. The
names here (`clientcert.pem`/`clientkey.pem`/`cacert.pem`) are an example; if you name them
differently (e.g. `remote-clientcert.pem`), use that name in **both** the `--from-file` key and the
matching `*_ref`. A mismatch fails ref resolution at provision time, after the host has already
registered.

Declare the host as a `[[remote_libvirt]]` instance (plus its `staged` base `[[image]]`) in the
`systems.toml` ConfigMap the deployment mounts:

```toml
[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"

[[remote_libvirt]]
name = "host"
uri = "qemu+tls://HOST.FQDN/system"
gdb_addr = "HOST.IP"
gdbstub_range = "47000:47099"
client_cert_ref = "clientcert.pem"
client_key_ref = "clientkey.pem"   # pragma: allowlist secret - filename ref
ca_cert_ref = "cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
```

The libvirt host knobs the inventory model does not carry stay env settings
(`KDIVE_REMOTE_LIBVIRT_{STORAGE_POOL,NETWORK,MACHINE}`). Leave `MACHINE` at its `pc` (i440fx)
default unless your host topology powers q35 pcie-root-ports correctly.

**Object-store reachability for the guest — `KDIVE_S3_ENDPOINT_URL` must be guest-routable.**
Both the remote **install** (the in-guest helper `curl`s the kernel bundle from a presigned GET)
and the two-phase **kdump capture** (the guest PUTs the vmcore to a presigned URL) mint the URL
against `KDIVE_S3_ENDPOINT_URL` and have the **guest** do the transfer. The presigned URL embeds
that endpoint host, so it must be a **control-plane address routable from the remote guest
network** — **not** `localhost`/loopback. The dev default `http://localhost:9000` is the *guest's*
own loopback, where no object store runs; the in-guest transfer then fails opaquely. If the object
store is a cluster-internal service (e.g. a ClusterIP MinIO), the guest cannot reach it — expose it
on a node-reachable address and set `KDIVE_S3_ENDPOINT_URL` to that, so pods and the guest resolve
the same endpoint.

The worker now **preflights** this (ADR-0110): a remote `install`/kdump `capture` against a
`localhost`/loopback `KDIVE_S3_ENDPOINT_URL` fails fast with a `configuration_error` naming the env
var, before any in-guest round-trip — instead of an opaque downstream curl failure. A
routable-looking endpoint the guest still cannot reach (a missed guest→store ACL) is *not* caught
by the preflight and surfaces as the in-guest transfer failure.

## 8. Validate

```bash
kdivectl doctor --provider remote-libvirt          # secret_ref → pass
```

The `secret_ref` check reports `pass` when the secret backend is healthy. With the **file-ref**
backend it counts *registered* refs, of which there are none (refs are resolved on demand, not
pre-registered) — so the detail reads `all 0 configured secret refs resolve`, which is the
expected pass, not a sign the TLS refs are missing. The authoritative check that the three TLS
refs actually resolve is the worker→host connect below.

`doctor --with-egress` reports a configuration error unless a probe-guest-backed diagnostics
factory is wired (deferred per ADR-0091/M2.4); that is expected, not a fault. To confirm the
worker→host TLS path directly, connect from the worker with the mounted secrets (all three PEMs in
one `pkipath` directory):

```bash
WK=$(kubectl -n kdive-demo get pod -l app=kdive-kdive-worker -o jsonpath='{.items[0].metadata.name}')
kubectl -n kdive-demo exec "$WK" -- python3 -c '
import os, tempfile, shutil, libvirt
src="/etc/kdive/secrets"; pki=tempfile.mkdtemp()
for f in ("cacert.pem","clientcert.pem","clientkey.pem"): shutil.copy(src+"/"+f, pki+"/"+f)
c=libvirt.openReadOnly("qemu+tls://HOST.FQDN/system?pkipath="+pki)
print("connected:", c.getHostname())
print("base volume present:", "fedora-kdive-remote-base-43.qcow2" in
      [v for p in c.listAllStoragePools() for v in p.listVolumes()])
'
```

A successful connect plus a visible base volume means registration, mutual TLS, the gdbstub ACL,
and image staging are all in place. Drive the end-to-end spine per
[remote-live-stack.md](remote-live-stack.md).

The `host_dump` capture method needs only a provisioned-to-`ready` System: `control.force_crash`
then `vmcore.fetch method=host_dump` dumps host-side and the **worker** uploads the core, so it
exercises the control and retrieve planes without an in-guest kdump kernel and without the guest
reaching the object store. The from-source kernel build (`runs.build` → `install` → `boot`, and
the `gdbstub`/`kdump`/`introspect.from_vmcore` legs that depend on a Run) needs a worker that is a
kernel-build host — a toolchain (`git`, `flex`, `bison`, `bc`, libelf/openssl headers) and a
`KDIVE_KERNEL_SRC` tree — which a lightweight app-pod worker is not.

### Offloading the from-source build to an ephemeral build VM (ADR-0100)

Instead of putting the toolchain on the worker, register an **ephemeral remote-libvirt build
host**: each server-lane build provisions a throwaway VM on this same remote-libvirt host, runs
`make` in-guest over the guest-agent exec channel, publishes the kernel bundle + `vmlinux` via
presigned PUT, and tears the VM down. The reconciler reaps a leaked builder by its
`kdive-build-<run_id>` domain marker + the owning BUILD job's liveness.

Operator steps:

1. **Stage a base build image** as a volume in the configured storage pool
   (`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`). It must carry the kernel toolchain (`git`, `flex`,
   `bison`, `bc`, libelf/openssl headers, `make`, `objcopy`, `tar`), the `qemu-guest-agent`
   (enabled at boot — provisioning waits for its channel), and `/bin/sh`/`curl`/`base64`. The
   build VM also needs network egress to the object store (the same guest→MinIO hop the
   `doctor --with-egress` check verifies; a host `FORWARD DROP` surfaces as a publish failure).
2. **Register the build host** (`platform_admin`):

   ```bash
   kdivectl tool call build_hosts.register_ephemeral_libvirt --json '{
     "name": "builders",
     "base_image_volume": "kdive-build-base.qcow2",
     "workspace_root": "/build", "max_concurrent": 2 }'
   ```

   `max_concurrent` bounds in-flight build leases; size the host's CPU/RAM/disk headroom above
   it, since a crash-leaked VM can briefly exceed it until the next reconciler sweep.
3. **Author the server profile** with `build_host: builders` and a git
   `kernel_source_ref` (`{"git": {"remote": "...", "ref": "v6.x"}}`) — an ephemeral host builds
   from a fresh clone, so a warm-tree string is rejected as `configuration_error`. Then
   `runs.build` → `install` → `boot` runs without a build toolchain on the worker.

## Appendix: kdivectl operator commands

`kdivectl` is the operator MCP client (it does not drive the agent lifecycle). Point it at the
server and authenticate (the demo mints a token; production brings its own via `KDIVE_TOKEN`):

```bash
export KDIVE_SERVER_URL=http://127.0.0.1:8000/mcp     # kubectl port-forward svc/<release>-server 8000:8000
export KDIVE_TOKEN=...                                 # operator token (azp → actor=operator-cli)

kdivectl resources list                 # and: allocations/systems/runs/jobs/inventory/secrets/fixtures
kdivectl doctor [--provider remote-libvirt]
kdivectl images list
kdivectl tool call <read-only-tool> --json '{}'        # fail-closed: non-read-only exits 3
# break-glass (role-gated, audited as operator-cli):
kdivectl resources cordon <id>                         # platform_operator
kdivectl teardown system <id> --reason R --force       # platform_admin
```

Exit codes: `0` ok, `1` generic, `2` configuration, `3` authorization-denied (or non-read-only
passthrough), `4` not-found, `5` conflict, `6` `doctor`-only check-could-not-run. Reads and
break-glass map the tool's `error_category` to these; a tool that raises is reported as a one-line
error (exit 1), not a traceback.
