# KDIVE systemd units

Run the KDIVE processes (`server` / `worker` / `reconciler`) as host services. System-scope
units are in [`system/`](system/); user-scope units are in [`user/`](user/). Backends
(Postgres, S3, OIDC) are external and are not ordered by these units.

## System scope

```bash
sudo useradd --system --home-dir /opt/kdive --shell /usr/sbin/nologin kdive
sudo install -d -o kdive -g kdive /etc/kdive
sudo install -m 0640 -o kdive -g kdive kdive.env.example /etc/kdive/kdive.env
# edit /etc/kdive/kdive.env: fill in KDIVE_* values and credentials
sudo cp system/kdive-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kdive-server kdive-worker kdive-reconciler
journalctl -u kdive-server -f
```

## User scope

```bash
install -d ~/.config/kdive
install -m 0640 kdive.env.example ~/.config/kdive/kdive.env
cp user/kdive-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kdive-server kdive-worker kdive-reconciler
```

Full prerequisites, the external-backend ordering note, and the env-file details are in
[the systemd operating guide](../../docs/operating/systemd.md).
