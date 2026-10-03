"""
Dashboard-only queue actions, layered on the pipeline's queue code.

The vfx-ingest-pipeline repo carries nothing for the web: everything the
dashboard adds (list/stats, retry/edit of failed jobs, audit trail) lives here.
Import after PIPELINE_ROOT and PIPELINE_ROOT/tools are on sys.path.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import package_queue as pq
from core.db import DatabaseManager
from core.queue_manager import QueueManager

# who retried/edited what from the web dashboard; lives in the ingest DB
_AUDIT = """
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL DEFAULT (datetime('now')),
    user    TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    job_id  INTEGER NOT NULL,
    action  TEXT    NOT NULL,
    detail  TEXT
);
"""


class WebQueue(QueueManager):
    """QueueManager plus the dashboard actions. Only ever touches failed jobs."""

    def __init__(self, db: DatabaseManager) -> None:
        super().__init__(db)
        with db.connect() as conn:
            conn.executescript(_AUDIT)

    def reset(self, job_id: int) -> bool:
        """Retry a failed job from scratch. Returns False if it isn't failed."""
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE ingest_jobs
                SET status = 'pending', retry_count = 0, error_message = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND status = 'failed'
                """,
                (job_id,),
            )
            return cur.rowcount == 1

    def edit(self, job_id: int, project: str, sequence: str, shot: str) -> bool:
        """Correct project/sequence/shot on a failed job. Returns False if it isn't failed."""
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE ingest_jobs
                SET project = ?, sequence = ?, shot = ?, updated_at = datetime('now')
                WHERE id = ? AND status = 'failed'
                """,
                (project, sequence, shot, job_id),
            )
            return cur.rowcount == 1

    def audit(self, user: str, kind: str, job_id: int, action: str, detail: str = "") -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO audit (user, kind, job_id, action, detail) VALUES (?,?,?,?,?)",
                (user, kind, job_id, action, detail),
            )

    def list_jobs(self, status: str | None = None, project: str | None = None,
                  q: str | None = None, since: float | None = None, until: float | None = None,
                  limit: int | None = 500) -> list[dict[str, Any]]:
        """since/until are epoch seconds on created_at (stored as UTC text). limit None = all."""
        sql, args = "SELECT * FROM ingest_jobs WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if project:
            sql += " AND project = ?"
            args.append(project)
        if q:
            sql += " AND (shot LIKE ? OR sequence LIKE ? OR source_path LIKE ?)"
            args += [f"%{q}%"] * 3
        if since is not None:
            sql += " AND created_at >= datetime(?, 'unixepoch')"
            args.append(since)
        if until is not None:
            sql += " AND created_at < datetime(?, 'unixepoch')"
            args.append(until)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(-1 if limit is None else limit)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def stats(self) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) n FROM ingest_jobs GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}


def package_list(status: str | None = None, project: str | None = None,
                 q: str | None = None, since: float | None = None, until: float | None = None,
                 limit: int | None = 500) -> list[dict[str, Any]]:
    """Package jobs, newest first. since/until are epoch seconds on created_at. limit None = all."""
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if project:
        sql += " AND project = ?"
        args.append(project)
    if q:
        sql += " AND (shot LIKE ? OR IFNULL(task, '') LIKE ?)"
        args += [f"%{q}%"] * 2
    if since is not None:
        sql += " AND created_at >= ?"
        args.append(since)
    if until is not None:
        sql += " AND created_at < ?"
        args.append(until)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(-1 if limit is None else limit)
    pq.init()
    with pq._db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def package_reset(job_id: int) -> bool:
    """Retry a failed package job. False if it isn't failed, or the same
    shot/task already has a live job (the unique index refuses it)."""
    pq.init()
    with pq._db() as c:
        try:
            cur = c.execute(
                "UPDATE jobs SET status=?, attempts=0, claimed_by=NULL, claimed_at=NULL,"
                " finished_at=NULL, result=NULL WHERE id=? AND status=?",
                (pq.PENDING, job_id, pq.FAILED))
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount == 1
