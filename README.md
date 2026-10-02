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
echo "WEB_SECRET=$(openssl rand -hex 32)" | sudo tee /etc/vfx-ingest/web.env && sudo chmod 600 /etc/vfx-ingest/web.env
# TLS cert/key from the studio CA at /etc/vfx-ingest/tls/{cert,key}.pem
sudo cp vfx-ingest-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vfx-ingest-web
```

## Local dev

```bash
PIPELINE_ROOT=D:/dev/5and8live/vfx-ingest-pipeline INGEST_CONFIG=... \
WEB_SECRET=x WEB_DEV_USER=me uvicorn api:app --port 8000
cd ui && npm run dev        # proxies /api to :8000
```

`WEB_DEV_USER` skips AD. Never set it in production.
