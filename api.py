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
    SHOTDECK_ROOT=/software/pipeline/sgdesk/sgdesk \
    uvicorn api:app --host 0.0.0.0 --port 8443 \
        --ssl-keyfile key.pem --ssl-certfile cert.pem

Everything private (session secret, admins, temp logins, mail recipients,
daemon host credentials) lives in one central file, WEB_CONFIG, default
/etc/vfx-ingest/.ingest-web.yaml. It must be mode 600: the server refuses to
start if anyone but the owner can read it. Layout is in README.md.

Login is Active Directory via ShotDeck's auth package (bind + ai-users group
check). Until AD is wired up, `temp_users` in the central config replaces it
with a fixed list of logins. Remove them once AD login works.
"""

from __future__ import annotations

import hmac
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from html import escape
from pathlib import Path

import yaml
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


def _load_private_config() -> dict:
    path = Path(os.environ.get("WEB_CONFIG", "/etc/vfx-ingest/.ingest-web.yaml"))
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SystemExit(f"{path} is readable by group/others — chmod 600 it")
    return yaml.safe_load(path.read_text()) or {}


conf = _load_private_config()
# systemd units shown in the Services strip
SERVICES = conf.get("services") or [
    "shotgun-event-daemon", "vfx-ingest-watcher", "vfx-ingest-worker", "package-worker"]
# logins allowed to start/restart services (needs the sudoers rule in README)
ADMINS = set(conf.get("admins") or [])
# who gets service-down / job-failed mail; empty = no alert mail
ALERT_TO = list(conf.get("alert_emails") or [])
ALERT_INTERVAL = int(conf.get("alert_interval", 30))
# every start/restart from the site is mailed here, alerts on or not
RESTART_NOTIFY = list(conf.get("restart_notify") or ["jitesh@5and8.ai"])
RESTART_LOG = Path(conf.get("restart_log") or LOG_DIR / "service_restarts.log")
# ponytail: plaintext temp logins; delete from the config when AD login is live
TEMP_USERS = {str(k): str(v) for k, v in (conf.get("temp_users") or {}).items()}
# Host the daemon runs on, for restarting it on another machine later. Read
# here so it has one home; nothing uses it yet. Prefer an SSH key over a password.
DAEMON_HOST = conf.get("daemon_host") or {}

app = FastAPI(title="5and8 Ingest Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=conf["secret_key"],
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
    if TEMP_USERS:
        expected = TEMP_USERS.get(body.username.strip(), "")
        if not expected or not hmac.compare_digest(expected, body.password):
            raise HTTPException(401, "wrong username or password")
        user = body.username.strip()
        request.session.update(user=user, name=user)
        return {"user": user, "name": user}
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
    return {"user": user, "name": request.session.get("name", user), "admin": user in ADMINS}


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


# ── services (daemon, watcher, workers) ───────────────────────────────────────

def _unit_state(unit: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit,
             "-p", "LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"name": unit, "state": "unknown"}      # no systemd (e.g. Windows dev box)
    p = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    state = "not-installed" if p.get("LoadState") == "not-found" else p.get("ActiveState") or "unknown"
    return {"name": unit, "state": state, "sub": p.get("SubState", ""),
            "since": p.get("ActiveEnterTimestamp", ""), "restarts": p.get("NRestarts", "")}


def _unit_or_404(unit: str) -> str:
    if unit not in SERVICES:
        raise HTTPException(404, "unknown service")
    return unit


@app.get("/api/services")
def services(_: str = Depends(current_user)):
    return [_unit_state(u) for u in SERVICES]


@app.get("/api/services/{unit}/log")
def service_log(unit: str, _: str = Depends(current_user)):
    # sgd needs to be in the systemd-journal group to read other units' journals
    try:
        out = subprocess.run(
            ["journalctl", "-u", _unit_or_404(unit), "-n", "150", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"log": [f"journal unavailable: {exc}"]}
    return {"log": (out.stdout or out.stderr).splitlines()}


@app.post("/api/services/{unit}/restart")
def service_restart(unit: str, request: Request, user: str = Depends(current_user)):
    """Start a stopped service or restart a running one (systemctl restart does both)."""
    if user not in ADMINS:
        raise HTTPException(403, "you are not allowed to restart services")
    _unit_or_404(unit)
    action = "restart" if _unit_state(unit)["state"] == "active" else "start"
    ip = request.client.host if request.client else "?"
    when = datetime.now().isoformat(timespec="seconds")
    # sudo -n: never prompt; the sudoers rule allows exactly these restarts
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                           capture_output=True, text=True, timeout=60)
        ok, err = r.returncode == 0, r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, err = False, str(exc)

    result = "ok" if ok else "FAILED"
    _log_restart(f"{when} user={user} ip={ip} action={action} unit={unit} result={result}"
                 + (f" error={err!r}" if err else ""))
    queue.audit(user, "service", 0, action if ok else f"{action}-failed", f"{unit} from {ip} {err}".strip())
    _mail(
        f"[ingest] {unit} {action} by {user}: {result}",
        f"<p><b>{escape(unit)}</b> {action} {result}</p>"
        f"<ul><li>By: {escape(user)}</li><li>From IP: {escape(ip)}</li><li>When: {when}</li>"
        + (f"<li>Error: {escape(err)}</li>" if err else "") + "</ul>",
        to=RESTART_NOTIFY + [a for a in ALERT_TO if a not in RESTART_NOTIFY],
    )
    if not ok:
        raise HTTPException(500, err or "restart failed")
    return _unit_state(unit)


def _log_restart(line: str) -> None:
    try:
        RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RESTART_LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"could not write {RESTART_LOG}: {exc} — {line}", file=sys.stderr)


# ── mail alerts ───────────────────────────────────────────────────────────────

def _mail(subject: str, body_html: str, to: list[str] | None = None) -> None:
    to = to if to is not None else ALERT_TO
    if not to:
        return
    try:
        from services.mailer import send_email      # pipeline's SMTP helper
        send_email(to, subject, body_html)
    except Exception as exc:
        print(f"alert mail failed: {exc}", file=sys.stderr)


def _failed_now() -> dict[tuple[str, int], str]:
    out = {("ingest", r["id"]): f"{r['project']}/{r['sequence']}/{r['shot']}: {r['error_message'] or ''}"
           for r in queue.list_jobs(status="failed")}
    out.update({("package", r["id"]): f"{r['project']}/{r['shot']} {r['task'] or ''}: {r['result'] or ''}"
                for r in pq.recent(500) if r["status"] == pq.FAILED})
    return out


def _monitor() -> None:
    """Mail when a service goes down/comes back, and when jobs newly fail."""
    states: dict[str, str] = {}
    seen = set(_failed_now())            # don't mail the backlog on startup
    while True:
        time.sleep(ALERT_INTERVAL)
        try:
            lines = []
            for s in map(_unit_state, SERVICES):
                prev, now = states.get(s["name"]), s["state"]
                states[s["name"]] = now
                if now == "unknown" or now == prev:
                    continue
                if now != "active" and prev in (None, "active"):
                    lines.append(f"<li><b>{escape(s['name'])} is {escape(now)}</b></li>")
                elif now == "active" and prev is not None:
                    lines.append(f"<li>{escape(s['name'])} is back up</li>")
            failed = _failed_now()
            new = sorted(set(failed) - seen)
            seen = set(failed)           # a retried job that fails again mails again
            lines += [f"<li>{kind} #{jid} failed — {escape(failed[(kind, jid)][:300])}</li>"
                      for kind, jid in new]
            if lines:
                _mail(f"[ingest] {len(lines)} alert(s)", "<ul>" + "".join(lines) + "</ul>")
        except Exception as exc:
            print(f"monitor error: {exc}", file=sys.stderr)


if ALERT_TO:
    threading.Thread(target=_monitor, daemon=True, name="alerts").start()


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
