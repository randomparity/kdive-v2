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

# (b) passt (appliance user-net) self-sandboxes via unprivileged userns, blocked by default
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/60-kdive-userns.conf
sudo sysctl --system

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
→ `passt exited with status 1` / `Failed to sandbox process`; `(c)` → eth0 stays `DOWN`, dnf
reports `Could not resolve host mirrors.fedoraproject.org`. `(b)` can alternatively be sidestepped
by removing passt so libguestfs falls back to qemu slirp, but `(c)` is required either way.

## 5. Build the operator-staged base image

The remote provider reaches the guest over **qemu-guest-agent** and boots a **bootable disk
image** (ADR-0078/0079/0080) — this is **not** the `python -m kdive build-rootfs` local-libvirt
artifact (which is a serial-readiness, whole-disk-ext4 image for direct-kernel boot). Build the
remote base image with the `RemoteLibvirtRootfsBuildPlane` recipe; the volume name must be
`fedora-kdive-remote-base-43.qcow2` (the provisioning profile derives it from
`REMOTE_BASE_IMAGE_NAME`).

```bash
virt-builder fedora-43 --format qcow2 --size 10G \
  --output /var/lib/libvirt/images/fedora-kdive-remote-base-43.qcow2 \
  --install qemu-guest-agent,drgn,kexec-tools,makedumpfile,kdump-utils \
  --run-command "systemctl enable qemu-guest-agent.service" \
  --run-command "systemctl enable kdump.service"
sudo virsh pool-refresh default
sudo virsh vol-list default
sha256sum /var/lib/libvirt/images/fedora-kdive-remote-base-43.qcow2   # the image identity
```

The matching `vmlinux`/debuginfo and a crashkernel-capable kernel remain the operator's content
contract (recorded in the plane's provenance); add them per your kdump needs.

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

Persist the rules with your firewall manager. The default gdbstub range is `47000..47099`
(`KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN/MAX`); `KDIVE_REMOTE_LIBVIRT_GDB_ADDR` has no default and
provisioning fails closed without it.

## 7. Register remote-libvirt on the deployment

The provider is opt-in: it registers only when `KDIVE_REMOTE_LIBVIRT_URI` is set. The TLS
materials are **secrets-by-reference** — the worker resolves the refs through the file-ref backend
under `KDIVE_SECRETS_ROOT` and materializes a per-op `pkipath`. The Helm chart projects a
Kubernetes Secret as files and sets `KDIVE_SECRETS_ROOT` for you.

```bash
# the client materials from step 2
kubectl -n kdive-demo create secret generic kdive-remote-tls \
  --from-file=clientcert.pem --from-file=clientkey.pem --from-file=cacert.pem

helm upgrade kdive deploy/helm/kdive -n kdive-demo --reuse-values \
  --set secrets.secretName=kdive-remote-tls \
  --set config.KDIVE_REMOTE_LIBVIRT_URI=qemu+tls://HOST.FQDN/system \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF=clientcert.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF=clientkey.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_CA_CERT_REF=cacert.pem \
  --set config.KDIVE_REMOTE_LIBVIRT_STORAGE_POOL=default \
  --set config.KDIVE_REMOTE_LIBVIRT_GDB_ADDR=HOST.IP
```

The full config surface is `KDIVE_REMOTE_LIBVIRT_{URI,CLIENT_CERT_REF,CLIENT_KEY_REF,CA_CERT_REF,
STORAGE_POOL,NETWORK,MACHINE,GDB_ADDR,GDB_PORT_MIN,GDB_PORT_MAX,ALLOCATION_CAP}`. Leave `MACHINE`
at its `pc` (i440fx) default unless your host topology powers q35 pcie-root-ports correctly.

**Object-store reachability for the guest.** The two-phase kdump capture has the **guest** PUT the
vmcore to a presigned `KDIVE_S3_ENDPOINT_URL`. If the object store is a cluster-internal service
(e.g. a ClusterIP MinIO), the guest cannot reach it — expose it on a node-reachable address and
set `KDIVE_S3_ENDPOINT_URL` to that, so pods and the guest resolve the same endpoint.

## 8. Validate

```bash
kdivectl doctor --provider remote-libvirt          # secret_ref → pass (the 3 refs resolve)
```

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
c=libvirt.openReadOnly(os.environ["KDIVE_REMOTE_LIBVIRT_URI"]+"?pkipath="+pki)
print("connected:", c.getHostname())
print("base volume present:", "fedora-kdive-remote-base-43.qcow2" in
      [v for p in c.listAllStoragePools() for v in p.listVolumes()])
'
```

A successful connect plus a visible base volume means registration, mutual TLS, the gdbstub ACL,
and image staging are all in place. Drive the end-to-end spine per
[remote-live-stack.md](remote-live-stack.md).

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
