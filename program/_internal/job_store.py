"""Local durable job snapshots. No model credentials are stored."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from credentials import is_sensitive_key, sensitive_fields

STATUS_FIELDS = ("run_id", "kind", "status", "phase", "progress", "label", "config", "error",
                 "created_at", "updated_at", "result_revision")


def job_metadata(job):
    """Bounded status view; never traverse result payloads during polling."""
    value = {key: job[key] for key in STATUS_FIELDS if key in job}
    value["events"] = job.get("events", [])[-20:]
    value["result_revision"] = job.get("result_revision", 0)
    value["has_result"] = bool(job.get("result"))
    value["has_partial_result"] = bool(job.get("partial_result"))
    return sanitize(value)


def compact_snapshot(value, context=None):
    """Database files and report.json retain SQL; checkpoints need business evidence only."""
    if isinstance(value, dict):
        private = sensitive_fields(value, context)
        return {key: compact_snapshot(item, key) for key, item in value.items()
                if key not in ("sqlite_dump", "sql_dump") and key not in private}
    if isinstance(value, (list, tuple)):
        return [compact_snapshot(item, context) for item in value]
    return value


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


def sanitize(value, context=None):
    if isinstance(value, dict):
        result = {}
        private = sensitive_fields(value, context)
        for key, item in value.items():
            if key in private:
                continue
            result[key] = sanitize(item, key)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item, context) for item in value]
    return value


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, snapshot TEXT NOT NULL)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "metadata" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
            # Small status rows live outside the large snapshot records. Updating
            # a column in the snapshot row can still rewrite SQLite overflow pages.
            db.execute("""CREATE TABLE IF NOT EXISTS job_status (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_recency ON jobs(created_at DESC,run_id DESC)")
            # One-time, data-only upgrade for older databases. No uploaded SQL is executed.
            for row in db.execute("SELECT run_id,snapshot FROM jobs WHERE metadata='{}'").fetchall():
                snapshot = json.loads(row["snapshot"])
                db.execute("UPDATE jobs SET metadata=?,snapshot=? WHERE run_id=?", (
                    json.dumps(job_metadata(snapshot), ensure_ascii=False, allow_nan=False),
                    json.dumps(compact_snapshot(snapshot), ensure_ascii=False, allow_nan=False), row["run_id"]))
            db.execute("""INSERT OR IGNORE INTO job_status(run_id,status,metadata)
                SELECT run_id,status,metadata FROM jobs WHERE run_id NOT IN (SELECT run_id FROM job_status)""")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def save(self, job, payload_changed=True):
        run_id = job.get("run_id")
        if not valid_id(run_id):
            raise ValueError("实验编号无效")
        timestamp = now()
        with closing(self.connect()) as db:
            old = db.execute("SELECT created_at FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            clean = job_metadata(job)
            clean["created_at"] = old["created_at"] if old else clean.get("created_at", timestamp)
            clean["updated_at"] = timestamp
            metadata = json.dumps(clean, ensure_ascii=False, allow_nan=False)
            if not old or payload_changed:
                snapshot = compact_snapshot(job)
                snapshot.update(created_at=clean["created_at"], updated_at=timestamp)
                encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                db.execute("""INSERT INTO jobs(run_id,kind,status,created_at,updated_at,snapshot,metadata)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET kind=excluded.kind,
                    status=excluded.status,updated_at=excluded.updated_at,snapshot=excluded.snapshot,metadata=excluded.metadata""",
                    (run_id, clean.get("kind", "experiment"), clean.get("status", "done"), clean["created_at"], timestamp, encoded, metadata))
            db.execute("""INSERT INTO job_status(run_id,status,metadata) VALUES (?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,metadata=excluded.metadata""",
                (run_id, clean.get("status", "running"), metadata))
            db.commit()
        return clean

    def get(self, run_id, lite=False):
        if not valid_id(run_id):
            return None
        with closing(self.connect()) as db:
            if lite:
                row = db.execute("SELECT metadata FROM job_status WHERE run_id=?", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT j.snapshot,s.metadata FROM jobs j JOIN job_status s ON s.run_id=j.run_id WHERE j.run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        # Older rows can predate the current credential policy. Read them
        # through the same boundary without rewriting the historical files.
        metadata = sanitize(json.loads(row["metadata"]))
        if lite:
            return metadata
        snapshot = sanitize(json.loads(row["snapshot"]))
        snapshot.update({key: value for key, value in metadata.items() if key != "events"})
        # Running snapshots can have newer status events than their last payload checkpoint.
        if snapshot.get("status") in ("running", "interrupted"):
            snapshot["events"] = metadata["events"]
        return snapshot

    def list(self, limit=50, offset=0):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("历史偏移量应为非负整数")
        with closing(self.connect()) as db:
            total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            rows = db.execute("SELECT s.metadata FROM jobs j JOIN job_status s ON s.run_id=j.run_id ORDER BY j.created_at DESC,j.run_id DESC LIMIT ? OFFSET ?",
                              (max(1, min(limit, 200)), min(offset, total))).fetchall()
        fields = ("run_id", "kind", "status", "created_at", "updated_at", "progress", "label", "config", "error")
        return {"items": [{k: value[k] for k in fields if k in value} for row in rows for value in [sanitize(json.loads(row[0]))]], "total": total}

    def recover_interrupted(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT run_id FROM job_status WHERE status='running'").fetchall()
        for row in rows:
            job = self.get(row[0])
            job.update(status="interrupted", phase="interrupted", error="服务在运行期间退出；保留最后检查点，未自动重跑或补发模型请求。")
            self.save(job)
        return len(rows)

    def import_legacy_reports(self, directory):
        """Read old JSON reports as data; never execute their SQLite dump text."""
        count = 0
        with closing(self.connect()) as db:
            existing_ids = {row[0] for row in db.execute("SELECT run_id FROM jobs")}
        for file in sorted(Path(directory).glob("*/report.json"))[:500]:
            run_id = file.parent.name
            if not valid_id(run_id) or run_id in existing_ids or file.stat().st_size > 8 * 1024 * 1024:
                continue
            try:
                report = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    continue
                kind = report.get("kind", "experiment")
                kind = kind if kind in ("study", "batch", "experiment", "concurrency", "test_suite") else "import"
                partial = bool(report.get("cancelled") or report.get("error") or report.get("status") == "partial" or report.get("stopped_reason"))
                created = datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).isoformat(timespec="milliseconds")
                self.save({"run_id": run_id, "kind": kind, "status": "error" if partial else "done",
                           "phase": "imported_history", "progress": 100 if not partial else 0, "events": [],
                           "label": "旧版实验记录", "created_at": created, "config": report.get("config", {}),
                           "partial_result" if partial else "result": report})
                count += 1
            except (ValueError, OSError, RecursionError):
                continue
        return count
