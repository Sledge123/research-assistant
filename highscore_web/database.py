"""单进程服务的持久 SQLite 队列；写入始终使用短事务。"""
from contextlib import contextmanager
import json
import sqlite3
import time
from .credits import Credits,SCHEMA as CREDIT_SCHEMA

DEFAULTS = {"license_confirmed": False, "cloud_validated": False, "public_enabled": False,
            "search_daily_limit": 0, "archive_daily_limit": 0}
ACTIVE = ("queued", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 verified INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL,
 expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
 state TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, request_key TEXT NOT NULL,
 database_id TEXT, archive_status TEXT NOT NULL DEFAULT 'not_started',
 mail_status TEXT NOT NULL DEFAULT 'not_requested', message TEXT NOT NULL DEFAULT '',
 results TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 worker_id TEXT, lease_hash TEXT, lease_expires REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 artifact_dir TEXT, artifact_expected TEXT, progress INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,request_key)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,created_at);
CREATE TABLE IF NOT EXISTS candidates (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), user_id TEXT NOT NULL,
 database_id TEXT NOT NULL, code TEXT NOT NULL, phase_name TEXT NOT NULL,
 chemical_formula TEXT, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidates_job ON candidates(job_id);
CREATE TABLE IF NOT EXISTS candidate_details (
 candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_usage (
 user_id TEXT NOT NULL, day TEXT NOT NULL, searches INTEGER NOT NULL DEFAULT 0,
 cards INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,day)
);
CREATE TABLE IF NOT EXISTS workers (
 id TEXT PRIMARY KEY, database_id TEXT NOT NULL, capabilities TEXT NOT NULL, last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_events (scope TEXT NOT NULL,key_hash TEXT NOT NULL,created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_lookup ON rate_events(scope,key_hash,created_at);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, user_id TEXT, action TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL);
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Database:
    def __init__(self, root):
        self.path = root / "output/web/state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            db.executescript(CREDIT_SCHEMA)
            from .admin_center import SCHEMA as ADMIN_SCHEMA
            db.executescript(ADMIN_SCHEMA)
            from .debug_accounts import SCHEMA as DEBUG_SCHEMA
            db.executescript(DEBUG_SCHEMA)
            from .research_library import SCHEMA as RESEARCH_LIBRARY_SCHEMA
            db.executescript(RESEARCH_LIBRARY_SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "artifact_expected" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN artifact_expected TEXT")
            for key, value in DEFAULTS.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, dumps(value)))

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def settings(db):
        return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM settings")}

    @staticmethod
    def expire(db):
        now = time.time()
        for row in db.execute("SELECT id FROM jobs WHERE kind='archive' AND state='running' AND lease_expires<?",(now,)).fetchall():
            Credits.settle_xrd(db,row['id'],0)
        db.execute("""UPDATE jobs SET state='needs_attention',updated_at=?,lease_hash=NULL,
          mail_status=CASE WHEN kind IN ('mail_retry','verification_mail') OR
          json_extract(payload,'$.send_mail')=1 THEN 'unknown' ELSE mail_status END,
          message='数据库执行器租约已过期，请管理员核查进程和投递状态后处理'
          WHERE state='running' AND lease_expires<?""", (now, now))
