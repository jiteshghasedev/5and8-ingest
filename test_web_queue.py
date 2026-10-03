"""
Self-check for the dashboard queue actions in web_queue.py.

    PIPELINE_ROOT=D:/dev/5and8/vfx-ingest-pipeline python test_web_queue.py
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

PIPELINE_ROOT = Path(os.environ.get("PIPELINE_ROOT", "/software/pipeline/vfx-ingest-pipeline"))
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.db import DatabaseManager      # noqa: E402
import web_queue                         # noqa: E402


def _queue(tmp: Path) -> web_queue.WebQueue:
    db = DatabaseManager(tmp / "ingest.db")
    db.init_db()
    with db.connect() as c:
        # this pipeline's schema lacks the column its update_status() writes;
        # the live DB has it, so add it to match
        if "original_plate_name" not in {r["name"] for r in c.execute("PRAGMA table_info(ingest_jobs)")}:
            c.execute("ALTER TABLE ingest_jobs ADD COLUMN original_plate_name TEXT")
    return web_queue.WebQueue(db)


def test_reset_edit_only_touch_failed(tmp: Path) -> None:
    q = _queue(tmp)
    ids = {s: q.enqueue("proj", "SEQ001", f"SH_{s}", f"/in/{s}") for s in
           ("failed", "copying", "completed")}
    for s, i in ids.items():
        q.update_status(i, s, error_message="boom" if s == "failed" else None)

    assert q.edit(ids["failed"], "proj2", "SEQ002", "SH999")
    assert q.reset(ids["failed"])
    job = q.get_job(ids["failed"])
    assert (job["status"], job["project"], job["shot"], job["error_message"], job["retry_count"]) \
        == ("pending", "proj2", "SH999", None, 0)

    for s in ("copying", "completed"):
        assert not q.reset(ids[s]), s
        assert not q.edit(ids[s], "x", "y", "z"), s
        assert q.get_job(ids[s])["status"] == s

    assert q.stats() == {"pending": 1, "copying": 1, "completed": 1}
    assert [j["id"] for j in q.list_jobs(q="SH999")] == [ids["failed"]]

    q.audit("jitesh", "ingest", ids["failed"], "retry")
    with q.db.connect() as c:
        assert c.execute("SELECT user FROM audit").fetchone()["user"] == "jitesh"


def test_created_date_filter(tmp: Path) -> None:
    import calendar
    import time
    q = _queue(tmp)
    old = q.enqueue("proj", "SEQ001", "SH_old", "/in/old")
    new = q.enqueue("proj", "SEQ001", "SH_new", "/in/new")
    with q.db.connect() as c:
        c.execute("UPDATE ingest_jobs SET created_at='2026-01-01 10:00:00' WHERE id=?", (old,))
    jan2 = calendar.timegm((2026, 1, 2, 0, 0, 0))
    assert [j["id"] for j in q.list_jobs(since=jan2)] == [new]
    assert [j["id"] for j in q.list_jobs(until=jan2)] == [old]
    assert len(q.list_jobs(limit=1)) == 1 and len(q.list_jobs(limit=None)) == 2

    os.environ["PACKAGE_QUEUE_DB"] = str(tmp / "pkg.db")
    pq = importlib.reload(web_queue.pq)
    a = pq.enqueue("Shot", 1, "proj", "SH010", "/cfg.yaml", task="mm")
    b = pq.enqueue("Shot", 2, "other", "SH020", "/cfg.yaml")
    with pq._db() as c:
        c.execute("UPDATE jobs SET created_at=? WHERE id=?", (jan2 - 3600, a))
    ids = lambda **k: [r["id"] for r in web_queue.package_list(**k)]
    assert ids(since=jan2) == [b] and ids(until=jan2) == [a]
    assert ids(project="proj") == [a] and ids(q="mm") == [a] and ids(status="pending") == [b, a]
    assert ids(since=time.time() + 60) == []


def test_package_reset(tmp: Path) -> None:
    os.environ["PACKAGE_QUEUE_DB"] = str(tmp / "pkg.db")
    pq = importlib.reload(web_queue.pq)
    j = pq.enqueue("Shot", 1, "proj", "SH010", "/cfg.yaml")
    assert not web_queue.package_reset(j)                       # pending, not failed
    with pq._db() as c:
        c.execute("UPDATE jobs SET status='failed', attempts=3 WHERE id=?", (j,))
    j2 = pq.enqueue("Shot", 1, "proj", "SH010", "/cfg.yaml")   # a live duplicate
    assert not web_queue.package_reset(j)                       # unique index refuses
    with pq._db() as c:
        c.execute("UPDATE jobs SET status='done' WHERE id=?", (j2,))
    assert web_queue.package_reset(j)
    assert pq.stats().get("pending") == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:  # Windows holds sqlite files
                fn(Path(d))
            print("ok", name)
