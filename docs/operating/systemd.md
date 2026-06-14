# Running KDIVE under systemd

Run the three KDIVE processes as host services. The unit files live under
[`deploy/systemd/`](../../deploy/systemd/): system-scope units in
[`deploy/systemd/system/`](../../deploy/systemd/system/) and user-scope units in
[`deploy/systemd/user/`](../../deploy/systemd/user/).

## External-backend prerequisite

The units run only the KDIVE processes. Postgres, the S3-compatible object store, and the
OIDC issuer are external and are not ordered by these units — a process retries until its
backends are reachable rather than failing terminally. If you co-locate a backend on the
same host, ordering the KDIVE units after it is the operator's responsibility: add the
appropriate `After=`/`Wants=` via a drop-in. Run the provider preflight (see
[install](install.md)) before the first start.

## System scope

Install the package under `/opt/kdive` with its `.venv`, create the service user, and place
the environment file:

```bash
sudo useradd --system --home-dir /opt/kdive --shell /usr/sbin/nologin kdive
sudo install -d -o kdive -g kdive /etc/kdive
sudo install -m 0640 -o kdive -g kdive \
  deploy/systemd/kdive.env.example /etc/kdive/kdive.env
```

Edit `/etc/kdive/kdive.env` and fill in the `KDIVE_*` values and credentials from your
secret store; the file ships credential-less by design. Every name in it is a registered
setting documented in [the config reference](../guide/reference/config.md). Keep the file
at mode 0640 owned by `kdive` so secrets are not world-readable.

Install and enable the units:

```bash
sudo cp deploy/systemd/system/kdive-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kdive-server kdive-worker kdive-reconciler
```

Follow the logs:

```bash
journalctl -u kdive-server -f
```

## User scope

The `--user` variant runs the same processes without root, reading the environment from
`~/.config/kdive/kdive.env` and the venv from `~/.local/share/kdive/.venv`:

```bash
install -d ~/.config/kdive
install -m 0640 deploy/systemd/kdive.env.example ~/.config/kdive/kdive.env
cp deploy/systemd/user/kdive-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kdive-server kdive-worker kdive-reconciler
journalctl --user -u kdive-server -f
```

A short install summary also lives next to the units in
[`deploy/systemd/README.md`](../../deploy/systemd/README.md).
