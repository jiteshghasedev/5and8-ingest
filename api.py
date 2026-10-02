"""
api.py — 5and8 ingest + packaging status dashboard.

Read-mostly view over the two SQLite queues. Ingest is still started by the
watcher and packaging by the `pkg` status in ShotGrid; this only shows status
and lets IO/production retry or correct a failed job.

Queue code (core/, tools/package_queue.py) is imported from the
vfx-ingest-pipeline checkout at PIPELINE_ROOT, so there is one copy of it.

    PIPELINE_ROOT=/software/pipeline/vfx-ingest-pipeline \
    INGEST_CONFIG=/software/pipeline/vfx-ingest-pipeline/config/ingest.yaml \
    PACKAGE_QUEUE_DB=/software/pipeline/queue/delivery/package_queue.db \
    WEB_SECRET=<random> SHOTDECK_ROOT=/software/pipeline/sgdesk/sgdesk \
    uvicorn api:app --host 0.0.0.0 --port 8443 \
        --ssl-keyfile key.pem --ssl-certfile cert.pem

Login is Active Directory via ShotDeck's auth package (bind + ai-users group
check). WEB_DEV_USER skips AD for local testing — never set it in production.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

PIPELINE_ROOT = Path(os.environ.get("PIPELINE_ROOT", "/software/pipeline/vfx-ingest-pipeline"))
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "tools"))

from core.config import load_config                 # noqa: E402
from core.db import DatabaseManager                 # noqa: E402
from core.queue_manager import QueueManager         # noqa: E402
import package_queue as pq                          # noqa: E402

cfg = load_config()
db = DatabaseManager(cfg["paths"]["sqlite_db_path"])
db.init_db()
queue = QueueManager(db)
LOG_DIR = Path(cfg["paths"]["log_path"])
PKG_LOG = Path(os.environ.get("PIPELINE_LOG_DIR", "/var/log/vfx-pipeline")) / "package_worker.log"
DEV_USER = os.environ.get("WEB_DEV_USER", "")

app = FastAPI(title="5and8 Ingest Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["WEB_SECRET"],
    max_age=12 * 3600,
    same_site="strict",
    https_only=True,
)


# ── auth ──────────────────────────────────────────────────────────────────────

class Login(BaseModel):
    username: str
    password: str


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "not logged in")
    return user


@app.post("/api/login")
def login(body: Login, request: Request):
    if DEV_USER:
        request.session.update(user=DEV_USER, name=DEV_USER)
        return {"user": DEV_USER, "name": DEV_USER}
    # authenticate_password, not authenticate(): the latter trusts the OS login
    # of the *server process*, which would let anyone in as sgd.
    sys.path.insert(0, os.environ.get("SHOTDECK_ROOT", "/software/pipeline/sgdesk/sgdesk"))
    from auth import authenticate_password
    result = authenticate_password(body.username, body.password)
    if not result.authorized:
        raise HTTPException(401, result.reason or "login failed")
    request.session.update(user=result.login, name=result.display_name)
    return {"user": result.login, "name": result.display_name}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {}


@app.get("/api/me")
def me(request: Request, user: str = Depends(current_user)):
    return {"user": user, "name": request.session.get("name", user)}


# ── read ──────────────────────────────────────────────────────────────────────

def _log_lines(path: Path, needle: str, limit: int = 300) -> list[str]:
    # ponytail: scans the whole log per request; fine until logs rotate (roadmap item 4)
    if not path.is_file():
        return []
    with open(path, errors="replace") as fh:
        lines = [ln.rstrip() for ln in fh if needle in ln]
    return lines[-limit:]


@app.get("/api/summary")
def summary(_: str = Depends(current_user)):
    return {"ingest": queue.stats(), "packages": pq.stats()}


@app.get("/api/ingest")
def ingest_list(status: str | None = None, project: str | None = None,
                q: str | None = None, _: str = Depends(current_user)):
    return queue.list_jobs(status=status, project=project, q=q)


@app.get("/api/ingest/{job_id}")
def ingest_detail(job_id: int, _: str = Depends(current_user)):
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    with db.connect() as conn:
        audit = [dict(r) for r in conn.execute(
            "SELECT * FROM audit WHERE kind='ingest' AND job_id=? ORDER BY id DESC",
            (job_id,))]
    return {
        "job": job,
        "log": _log_lines(LOG_DIR / "worker.log", f"[Job {job_id}]"),
        "audit": audit,
        "source_exists": Path(job["source_path"]).exists(),
    }


@app.get("/api/packages")
def package_list(_: str = Depends(current_user)):
    return [dict(r) for r in pq.recent(500)]


@app.get("/api/packages/{job_id}")
def package_detail(job_id: int, _: str = Depends(current_user)):
    with pq._db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    row = dict(row)
    with db.connect() as conn:
        audit = [dict(r) for r in conn.execute(
            "SELECT * FROM audit WHERE kind='package' AND job_id=? ORDER BY id DESC",
            (job_id,))]
    return {"job": row, "log": _log_lines(PKG_LOG, f" job.{job_id}: "), "audit": audit}


# ── actions: failed jobs only ─────────────────────────────────────────────────

class IngestEdit(BaseModel):
    project: str
    sequence: str
    shot: str


@app.post("/api/ingest/{job_id}/retry")
def ingest_retry(job_id: int, user: str = Depends(current_user)):
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not Path(job["source_path"]).exists():
        raise HTTPException(409, f"source is gone: {job['source_path']}")
    if not queue.reset(job_id):
        raise HTTPException(409, f"job is {job['status']}, only failed jobs can be retried")
    queue.audit(user, "ingest", job_id, "retry", job.get("error_message") or "")
    return queue.get_job(job_id)


@app.patch("/api/ingest/{job_id}")
def ingest_edit(job_id: int, body: IngestEdit, user: str = Depends(current_user)):
    fields = {k: v.strip() for k, v in body.model_dump().items()}
    if not all(fields.values()) or any("/" in v or ".." in v for v in fields.values()):
        raise HTTPException(422, "project, sequence and shot must be plain names")
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not queue.edit(job_id, **fields):
        raise HTTPException(409, f"job is {job['status']}, only failed jobs can be edited")
    before = f"{job['project']}/{job['sequence']}/{job['shot']}"
    after = "/".join(fields.values())
    queue.audit(user, "ingest", job_id, "edit", f"{before} -> {after}")
    return queue.get_job(job_id)


@app.post("/api/packages/{job_id}/retry")
def package_retry(job_id: int, user: str = Depends(current_user)):
    if not pq.reset(job_id):
        raise HTTPException(
            409, "only failed jobs can be retried, and not while the same shot has a live job")
    queue.audit(user, "package", job_id, "retry")
    return {"ok": True}


# Built React app. Mounted last so /api/* wins.
_dist = Path(__file__).parent / "ui" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
