# Deploy on Rocky Linux — 5and8 Ingest Dashboard

The dashboard runs on the same machine as the ShotGrid event daemon and the
ingest/package workers, as a systemd unit, the same way `shotgun-event-daemon`
runs: code under `/software/pipeline`, a Python venv, secrets in a mode-600 file
under `/etc/vfx-ingest`, logs in the journal.

It is deliberately not a container. It imports the pipeline's code from
`/software/pipeline/vfx-ingest-pipeline`, writes to the same SQLite queues as
the workers, and its Services panel runs `systemctl`/`journalctl` against the
host's units. A container would need all of that mounted back in, and the
restart buttons would need the host's systemd socket, which is root on the host.

Every step runs on the daemon machine as a user with `sudo`, except step 1,
which runs on the Windows dev box.

Paths used throughout:

| What | Where |
|---|---|
| Dashboard code | `/software/pipeline/5and8-ingest` |
| Pipeline code + venv | `/software/pipeline/vfx-ingest-pipeline`, `.../ingest_venv` |
| Ingest queue DB | `/software/pipeline/vfx-ingest-pipeline/db/ingest.db` (from `config/ingest.yaml`) |
| Package queue DB | `/software/pipeline/queue/delivery/package_queue.db` |
| Package worker log | `/software/pipeline/queue/delivery/logs/package_worker.log` |
| Private config | `/etc/vfx-ingest/.ingest-web.yaml` |
| TLS cert/key | `/etc/vfx-ingest/tls/cert.pem`, `key.pem` |
| Restart audit log | `/var/log/vfx-ingest/service_restarts.log` |
| Unit | `/etc/systemd/system/vfx-ingest-web.service` |
| URL | `https://<daemon-host>:8443` |

---

## 0. Check the prerequisites

The pipeline must already be deployed and running on this machine. The
dashboard only reads and retries its jobs.

```bash
systemctl is-active shotgun-event-daemon vfx-ingest-watcher vfx-ingest-worker package-worker
ls -ld /software/pipeline/vfx-ingest-pipeline/ingest_venv
id sgd                       # service user, must be in ai-users
getent group ai-users
ls -ld /etc/vfx-ingest       # holds sg.env already
```

The pipeline checkout must be on the branch that has the dashboard's
dependencies (`dev_20260802` or later): `core/queue_manager.py` with the
`original_plate_name` column and `tools/package_queue.py` with the `sequence`
column.

```bash
git -C /software/pipeline/vfx-ingest-pipeline log --oneline -1
```

---

## 1. Build the UI and push (Windows dev box)

The server has no Node, so the built UI (`ui/dist`) is committed to git.
Rebuild it whenever anything under `ui/src` changes.

```powershell
cd D:\dev\5and8\5and8-ingest\ui
npm install
npm run build
cd ..
$env:PIPELINE_ROOT = "D:\dev\5and8\GIT\vfx-ingest-pipeline"
D:\dev\5and8\.venv\Scripts\python.exe test_web_queue.py     # every line "ok"
git add -A
git status          # no .ingest-web*.yaml, no keys
git commit -m "..."
git push origin main
```

---

## 2. Put the code on the server

```bash
sudo git clone https://github.com/jiteshghasedev/5and8-ingest.git /software/pipeline/5and8-ingest
sudo chown -R sgd:ai-users /software/pipeline/5and8-ingest
```

The repository is on GitHub. If it is private, git asks for a username and a
personal access token (read-only, `Contents: read`), not your password. A
deploy key works too.

No git access from the server? Copy from the dev box instead. This runs in
PowerShell on the dev machine:

```powershell
ssh user@<daemon-host> mkdir -p /tmp/5and8-ingest
scp -r D:\dev\5and8\5and8-ingest\api.py D:\dev\5and8\5and8-ingest\web_queue.py `
       D:\dev\5and8\5and8-ingest\vfx-ingest-web.service D:\dev\5and8\5and8-ingest\ui `
       user@<daemon-host>:/tmp/5and8-ingest/
```

then on the server (leave out `ui/node_modules`, the server doesn't need it):

```bash
sudo mkdir -p /software/pipeline/5and8-ingest
sudo cp -r /tmp/5and8-ingest/. /software/pipeline/5and8-ingest/
sudo rm -rf /software/pipeline/5and8-ingest/ui/node_modules /tmp/5and8-ingest
sudo chown -R sgd:ai-users /software/pipeline/5and8-ingest
```

Check that the built UI arrived:

```bash
ls /software/pipeline/5and8-ingest/ui/dist/index.html
```

---

## 3. Install the Python packages into the pipeline venv

The dashboard reuses `ingest_venv`, so there is one Python for the whole
pipeline.

```bash
V=/software/pipeline/vfx-ingest-pipeline/ingest_venv/bin/python
sudo -u sgd $V -m pip install fastapi uvicorn itsdangerous ldap3
sudo -u sgd $V -c "import fastapi, uvicorn, itsdangerous, ldap3, yaml; print('deps ok')"
```

Check that the dashboard can load the pipeline code it imports:

```bash
cd /software/pipeline/5and8-ingest
sudo -u sgd env PIPELINE_ROOT=/software/pipeline/vfx-ingest-pipeline $V -c "
import sys; sys.path[:0] = ['/software/pipeline/vfx-ingest-pipeline', '/software/pipeline/vfx-ingest-pipeline/tools']
import web_queue; print('pipeline import ok')"
```

---

## 4. Create the private config

Everything private lives in one file. It must be owned by `sgd` and mode 600.
The server refuses to start if group or others can read it.

```bash
openssl rand -hex 32         # copy this for secret_key
sudo install -o sgd -g sgd -m 600 /dev/null /etc/vfx-ingest/.ingest-web.yaml
sudoedit /etc/vfx-ingest/.ingest-web.yaml
```

Contents:

```yaml
secret_key: <paste the openssl output>   # signs login cookies; changing it logs everyone out
admins: [jitesh]                         # may start/restart services from the site
temp_users:                              # TEMPORARY logins until AD login works; delete then
  jitesh: <strong password>
  io: <strong password>
alert_emails: [io@5and8.ai]              # service down/up + newly failed jobs; [] = off
alert_interval: 30                       # seconds between checks
restart_notify: [jitesh@5and8.ai]        # mailed on every start/restart from the site
restart_log: /var/log/vfx-ingest/service_restarts.log
services:                                # units shown in the Services panel
  - shotgun-event-daemon
  - vfx-ingest-watcher
  - vfx-ingest-worker
  - package-worker
```

Do not reuse the passwords from the local test setup.

```bash
sudo ls -l /etc/vfx-ingest/.ingest-web.yaml     # expect -rw------- sgd sgd
```

---

## 5. TLS certificate

Login cookies are HTTPS-only, so the site must be served over HTTPS. Browsers
only make an exception for `localhost`.

Preferred: a cert from the studio CA for the machine's hostname. Put it at
`/etc/vfx-ingest/tls/cert.pem` (full chain) and `key.pem`.

Fallback, self-signed (browsers show a warning once per user):

```bash
H=$(hostname -f); IP=$(hostname -I | awk '{print $1}')
sudo mkdir -p /etc/vfx-ingest/tls
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
     -keyout /etc/vfx-ingest/tls/key.pem -out /etc/vfx-ingest/tls/cert.pem \
     -subj "/CN=$H" -addext "subjectAltName=DNS:$H,IP:$IP"
```

Either way:

```bash
sudo chown -R sgd:sgd /etc/vfx-ingest/tls
sudo chmod 600 /etc/vfx-ingest/tls/key.pem
sudo chmod 644 /etc/vfx-ingest/tls/cert.pem
```

---

## 6. Log directory

```bash
sudo mkdir -p /var/log/vfx-ingest
sudo chown sgd:ai-users /var/log/vfx-ingest
```

---

## 7. Let `sgd` see and restart the pipeline units

Read other units' journals (the Services panel's log view):

```bash
sudo usermod -aG systemd-journal sgd
```

Restart exactly the four pipeline units, nothing else:

```bash
sudo tee /etc/sudoers.d/vfx-ingest-web >/dev/null <<'EOF'
sgd ALL=(root) NOPASSWD: /usr/bin/systemctl restart shotgun-event-daemon, /usr/bin/systemctl restart vfx-ingest-watcher, /usr/bin/systemctl restart vfx-ingest-worker, /usr/bin/systemctl restart package-worker
EOF
sudo chmod 440 /etc/sudoers.d/vfx-ingest-web
sudo visudo -c                  # must say "parsed OK"
sudo -u sgd sudo -n -l          # lists the four restart commands
```

If you add a unit to `services:` in step 4, add it here too, or its restart
button fails.

---

## 8. Open the firewall

```bash
sudo firewall-cmd --permanent --add-port=8443/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports  # 8443/tcp listed
```

---

## 9. Test in the foreground first

Run it with the same environment the unit uses, before installing the unit:

```bash
cd /software/pipeline/5and8-ingest
sudo -u sgd env \
  PIPELINE_ROOT=/software/pipeline/vfx-ingest-pipeline \
  INGEST_CONFIG=/software/pipeline/vfx-ingest-pipeline/config/ingest.yaml \
  PACKAGE_QUEUE_DB=/software/pipeline/queue/delivery/package_queue.db \
  PIPELINE_LOG_DIR=/software/pipeline/queue/delivery/logs \
  SHOTDECK_ROOT=/software/pipeline/sgdesk/sgdesk \
  WEB_CONFIG=/etc/vfx-ingest/.ingest-web.yaml \
  /software/pipeline/vfx-ingest-pipeline/ingest_venv/bin/python -m uvicorn api:app \
    --host 0.0.0.0 --port 8443 \
    --ssl-keyfile /etc/vfx-ingest/tls/key.pem --ssl-certfile /etc/vfx-ingest/tls/cert.pem
```

`Application startup complete` means it is up. In a second SSH session:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/                # 200
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/api/summary     # 401 (not logged in)
curl -sk -c /tmp/ck -H 'Content-Type: application/json' \
     -d '{"username":"jitesh","password":"<password>"}' https://localhost:8443/api/login
curl -sk -b /tmp/ck https://localhost:8443/api/summary                            # job counts
curl -sk -b /tmp/ck https://localhost:8443/api/services                           # four units, "active"
curl -sk -b /tmp/ck https://localhost:8443/api/export/ingest | head -3            # CSV header + rows
rm -f /tmp/ck
```

Stop it with `Ctrl+C`.

---

## 10. Install the systemd unit

The unit ships in the repo as `vfx-ingest-web.service` and already has the
paths above.

```bash
sudo cp /software/pipeline/5and8-ingest/vfx-ingest-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vfx-ingest-web
systemctl status vfx-ingest-web
```

Logs go to the journal:

```bash
journalctl -u vfx-ingest-web -f          # follow
journalctl -u vfx-ingest-web -n 100      # last 100 lines
```

It starts at boot and restarts itself 10 seconds after a crash.

---

## 11. Check it from a workstation

Open `https://<daemon-host>:8443` and:

1. Log in with a `temp_users` login.
2. **Ingest** and **Packaging** tabs show the real queues. Filter by status,
   project, search and **Created date**.
3. **Export CSV** downloads the filtered rows and opens in Excel.
4. Services panel: all four units green. As an admin, restart
   `vfx-ingest-watcher` once. Check that the mail to `restart_notify` arrives
   and that a line appears in `/var/log/vfx-ingest/service_restarts.log`.

---

## Updating

On the dev box: change, rebuild the UI if `ui/src` changed, run the test,
commit, push (step 1). Then on the server:

```bash
sudo -u sgd git -C /software/pipeline/5and8-ingest pull
sudo systemctl restart vfx-ingest-web
journalctl -u vfx-ingest-web -n 20
```

A new Python package means step 3 again before the restart. A change to the
unit file means `cp` + `daemon-reload` from step 10.

Changing `.ingest-web.yaml` also needs `sudo systemctl restart vfx-ingest-web`;
it is read only at startup.

## Rolling back

```bash
sudo -u sgd git -C /software/pipeline/5and8-ingest log --oneline -5
sudo -u sgd git -C /software/pipeline/5and8-ingest checkout <previous-commit>
sudo systemctl restart vfx-ingest-web
```

The dashboard never changes the queue schemas beyond adding its own `audit`
table, so rolling it back does not affect the daemon or workers.

## When AD login works

Delete `temp_users` from `.ingest-web.yaml` and restart. Login then goes
through ShotDeck's `auth` package at `SHOTDECK_ROOT` (bind + `ai-users` group
check). With `temp_users` set, AD is not used at all.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Unit exits: `.ingest-web.yaml is readable by group/others` | `sudo chmod 600 /etc/vfx-ingest/.ingest-web.yaml` |
| Unit exits: `KeyError: 'secret_key'` | `secret_key` missing from the config |
| `status=203/EXEC` | Wrong venv path in `ExecStart`; check `ls -l .../ingest_venv/bin/python` |
| `PermissionError` on a `.db` file | `sgd` can't write the queue DB or its directory; same owner as the workers |
| Login works, then every page says "not logged in" | Opened over `http://`; use `https://` |
| Browser can't connect, `curl` on the server works | Firewall, step 8 |
| Services panel shows `unknown` | `systemctl` not reachable; only expected on a Windows dev box |
| Service log says `No journal files were found` | `sgd` not in `systemd-journal` (step 7); restart the unit after `usermod` |
| Restart button: `sudo: a password is required` | Sudoers rule missing or unit name differs (step 7) |
| No alert mail | `alert_emails` empty, or SMTP unreachable: mail uses the pipeline's `services/mailer.py`, `SMTP_*` env vars, default `smtp01.5and8.net:55` |
