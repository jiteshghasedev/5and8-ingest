# 5and8 Ingest Dashboard

Live status of ingest and packaging jobs for producers, coordinators and IO.
Shows errors and per-job log lines, and lets `ai-users` retry or correct
failed jobs. Ingest is still started by the watcher/daemon, never from here.

- `api.py`: FastAPI. Reads the queues through the `vfx-ingest-pipeline`
  checkout at `PIPELINE_ROOT` (`core/queue_manager.py`, `tools/package_queue.py`).
- `ui/`: React + Vite. `ui/dist` is the built app that `api.py` serves.
- Login is Active Directory via ShotDeck's `auth` package (`SHOTDECK_ROOT`).

## Deploy (Rocky)

```bash
# build the UI on a box with Node, then commit/copy ui/dist
cd ui && npm install && npm run build

# server: code at /software/pipeline/5and8-ingest, reuses the pipeline venv
/software/pipeline/vfx-ingest-pipeline/ingest_venv/bin/pip install fastapi uvicorn itsdangerous ldap3
# central private config, see below
sudo install -o sgd -g sgd -m 600 /dev/null /etc/vfx-ingest/.ingest-web.yaml
sudoedit /etc/vfx-ingest/.ingest-web.yaml
# TLS cert/key from the studio CA at /etc/vfx-ingest/tls/{cert,key}.pem
sudo cp vfx-ingest-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vfx-ingest-web
```

## Central private config

One file holds everything private: `/etc/vfx-ingest/.ingest-web.yaml`
(override with `WEB_CONFIG`). It must be `chmod 600`, owner `sgd`; the server
refuses to start if group or others can read it. Never commit it (it is in
`.gitignore`).

```yaml
secret_key: <openssl rand -hex 32>     # signs login cookies
admins: [jitesh]                       # may start/restart services from the site
temp_users:                            # TEMPORARY logins until AD works; delete then
  jitesh: <password>
alert_emails: [io@5and8.ai]            # service down/up + newly failed jobs; [] = off
alert_interval: 30                     # seconds between checks
restart_notify: [jitesh@5and8.ai]      # always mailed on every start/restart
restart_log: /var/log/vfx-ingest/service_restarts.log
services:                              # optional, these are the defaults
  - shotgun-event-daemon
  - vfx-ingest-watcher
  - vfx-ingest-worker
  - package-worker
daemon_host:                           # for restarting the daemon on another machine later; unused yet
  ip: ""
  username: ""
  password: ""                         # prefer an SSH key: leave empty and use key auth
```

## Services: status, start/restart, log, mail

The site shows `systemctl` state for `services` and their last journal lines.
Logins in `admins` get a Start (stopped) / Restart (running) button. Every
use is:

- appended to `restart_log`: time, user, client IP, action, unit, result
- recorded in the `audit` table
- mailed to `restart_notify` (plus `alert_emails`)

```bash
# let sgd read other units' journals
sudo usermod -aG systemd-journal sgd

# let sgd restart exactly these units, nothing else
sudo tee /etc/sudoers.d/vfx-ingest-web <<'EOF'
sgd ALL=(root) NOPASSWD: /usr/bin/systemctl restart shotgun-event-daemon,     /usr/bin/systemctl restart vfx-ingest-watcher,     /usr/bin/systemctl restart vfx-ingest-worker,     /usr/bin/systemctl restart package-worker
EOF
sudo chmod 440 /etc/sudoers.d/vfx-ingest-web && sudo visudo -c
```

Mail goes through the pipeline's `services/mailer.py` (`SMTP_*` env vars,
default `smtp01.5and8.net:55`).

## Local dev

```bash
PIPELINE_ROOT=D:/dev/5and8live/vfx-ingest-pipeline INGEST_CONFIG=... WEB_CONFIG=D:/somewhere/.ingest-web.yaml uvicorn api:app --port 8000
cd ui && npm run dev        # proxies /api to :8000
```
