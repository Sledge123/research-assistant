"""两类科学任务共用一个数据库计算名额，领取必须处于短写事务。"""
import time

KINDS = {'abtem': ('abtem_jobs', 'abtem_workers'),
         'pymatgen': ('pymatgen_jobs', 'pymatgen_workers')}


def register(db, kind, enabled):
    if kind not in KINDS:
        raise ValueError('未知科学任务类型。')
    db.execute('CREATE TABLE IF NOT EXISTS science_queue_roles (kind TEXT PRIMARY KEY,enabled INTEGER NOT NULL)')
    db.execute('INSERT INTO science_queue_roles VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET enabled=excluded.enabled',
               (kind, int(enabled)))


def maintain(service, db):
    register(db, service.science_kind, service.enabled)
    service.maintain(db)
    for peer in getattr(service, 'science_peers', ()):
        register(db, peer.science_kind, peer.enabled)
        peer.maintain(db)


def claimable(db, kind):
    if kind not in KINDS or not db.in_transaction:
        raise RuntimeError('科学任务领取须在写事务内执行。')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    candidates, now = [], time.time()
    for current, (jobs, workers) in KINDS.items():
        if jobs not in tables or workers not in tables:
            continue
        # 包含失效租约；对应维护确认其业务终态后才可让出数据库名额。
        # Linux flock 独立于业务状态，残留进程不会因该维护而让出物理名额。
        if db.execute(f"SELECT 1 FROM {jobs} WHERE state='running' LIMIT 1").fetchone():
            return False
        enabled = db.execute('SELECT enabled FROM science_queue_roles WHERE kind=?', (current,)).fetchone()
        if enabled is not None and not enabled[0]:
            continue
        if not db.execute(f'SELECT 1 FROM {workers} WHERE available=1 AND last_seen>? LIMIT 1', (now - 90,)).fetchone():
            continue
        row = db.execute(f"SELECT created_at,id FROM {jobs} WHERE state='queued' AND queue_deadline>? ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
        if row is not None:
            candidates.append((row['created_at'], row['id'], current))
    return bool(candidates) and min(candidates)[2] == kind
