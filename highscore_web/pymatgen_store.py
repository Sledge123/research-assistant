"""材料工具独立队列；通过全站短事务冻结和结算积分。"""
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import zipfile

from highscore_pymatgen.contract import (DEFAULT_PARAMETERS, LIMITS, MAX_INPUT_BYTES,
    RETENTION_SECONDS, validate_inputs, validate_parameters)
from highscore_pymatgen.artifacts import MAX_BUNDLE_BYTES, validate_bundle
from .database import dumps
from .credits import Credits
from .security import digest, new_token
from . import science_queue

LEASE_SECONDS = 300
QUEUE_SECONDS = 86400
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
COSTS = {'analyze': 1, 'convert': 1, 'xrd': 2, 'supercell': 1, 'compare': 1}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS pymatgen_jobs (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),request_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL,operation TEXT NOT NULL,state TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'queued',
 parameters TEXT NOT NULL,source_json TEXT NOT NULL,input_name TEXT NOT NULL,
 created_at REAL NOT NULL,updated_at REAL NOT NULL,queue_deadline REAL NOT NULL,
 started_at REAL,finished_at REAL,run_deadline REAL,download_expires_at REAL,
 progress INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0,
 file_expired INTEGER NOT NULL DEFAULT 0,worker_id TEXT,lease_hash TEXT,lease_until REAL,
 completion_lease_hash TEXT,completion_state TEXT,artifact_path TEXT,artifact_sha256 TEXT,
 artifact_size INTEGER,result_summary TEXT,message TEXT NOT NULL DEFAULT '',failure_code TEXT,
 reserved_credits INTEGER NOT NULL DEFAULT 0,credit_status TEXT NOT NULL DEFAULT 'not_required',
 UNIQUE(user_id,request_key)
);
CREATE INDEX IF NOT EXISTS pymatgen_queue ON pymatgen_jobs(state,created_at);
CREATE INDEX IF NOT EXISTS pymatgen_owner ON pymatgen_jobs(user_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS pymatgen_owner_active ON pymatgen_jobs(user_id) WHERE state IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS pymatgen_global_running ON pymatgen_jobs((1)) WHERE state='running';
CREATE TABLE IF NOT EXISTS pymatgen_inputs (
 job_id TEXT NOT NULL REFERENCES pymatgen_jobs(id),position INTEGER NOT NULL,
 filename TEXT NOT NULL,sha256 TEXT NOT NULL,content BLOB,PRIMARY KEY(job_id,position)
);
CREATE TABLE IF NOT EXISTS pymatgen_workers (id TEXT PRIMARY KEY,last_seen REAL NOT NULL,available INTEGER NOT NULL);
'''


class PymatgenError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class PymatgenStore:
    science_kind = 'pymatgen'

    def __init__(self, root, database, *, enabled=False, archive_provider=None):
        self.root, self.database = Path(root).resolve(), database
        self.enabled, self.archive_provider = enabled is True, archive_provider
        with database.connection() as db:
            db.executescript(SCHEMA)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(pymatgen_jobs)')}
            # 旧任务保持免费，包括升级前尚在排队或计算中的任务。
            for name, declaration in (('reserved_credits', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('credit_status', "TEXT NOT NULL DEFAULT 'not_required'")):
                if name not in columns:
                    db.execute('ALTER TABLE pymatgen_jobs ADD COLUMN ' + name + ' ' + declaration)
            science_queue.register(db, 'pymatgen', self.enabled)

    @staticmethod
    def audit(db, row, action, detail=None):
        db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)',
                   (row['user_id'], 'pymatgen_' + action,
                    dumps({'job_id': row['id'], **(detail or {})}), time.time()))

    @staticmethod
    def own(db, job_id, user_id):
        row = db.execute('SELECT * FROM pymatgen_jobs WHERE id=? AND user_id=?', (job_id, user_id)).fetchone()
        if row is None:
            raise PymatgenError(404, '晶体工具任务不存在。')
        return row

    def sources(self, db, user_id, offset=0, limit=50):
        if self.archive_provider is None:
            return {'items': [], 'next_offset': None}
        return self.archive_provider.sources(db, user_id, offset, limit)

    def resolve_inputs(self, db, user_id, sources, operation, parameters):
        try:
            params = validate_parameters(operation, parameters)
            if not isinstance(sources, list) or len(sources) != (2 if operation == 'compare' else 1):
                raise ValueError('结构比较需要两个文件，其他工具需要一个文件。')
            inputs, origins = [], []
            for source in sources:
                if source['type'] == 'upload':
                    encoded = source['content_base64']
                    if not isinstance(encoded, str) or len(encoded) > (MAX_INPUT_BYTES + 2) // 3 * 4:
                        raise ValueError('每个结构文件不得超过512 KiB。')
                    content, name = base64.b64decode(encoded, validate=True), source['filename']
                    origin = {'type': 'upload', 'filename': name}
                else:
                    if self.archive_provider is None or not re.fullmatch(r'ICSD-[0-9]{6}', source['code']):
                        raise ValueError('请选择本人已生成的结构 CIF。')
                    try:
                        content = self.archive_provider.archive_input(db, user_id, source['job_id'], source['code'])
                    except ValueError as exc:
                        if hasattr(exc, 'status'):
                            raise PymatgenError(exc.status, str(exc)) from None
                        raise
                    name = source['code'] + '.cif'
                    origin = {'type': 'archive', 'job_id': source['job_id'], 'code': source['code']}
                inputs.append({'filename': name, 'content': content})
                origins.append(origin)
            validate_inputs(inputs, operation)
        except PymatgenError:
            raise
        except (ValueError, binascii.Error, KeyError, TypeError) as exc:
            raise PymatgenError(422, str(exc) if isinstance(exc, ValueError) else '结构输入格式无效。') from None
        identity = {'operation': operation, 'parameters': params, 'sources': origins,
                    'inputs': [{'filename': item['filename'], 'sha256': sha256(item['content'])} for item in inputs]}
        return {**identity, 'inputs': inputs, 'fingerprint': sha256(dumps(identity).encode('utf-8'))}

    def replay(self, db, user_id, request_key, sources, operation, parameters):
        row = db.execute('SELECT * FROM pymatgen_jobs WHERE user_id=? AND request_key=?', (user_id, request_key)).fetchone()
        if row is None:
            return None
        try:
            params = validate_parameters(operation, parameters)
        except ValueError as exc:
            raise PymatgenError(422, str(exc)) from None
        if operation != row['operation'] or params != json.loads(row['parameters']):
            raise PymatgenError(409, '请求标识已用于不同的结构或工具参数。')
        originals = json.loads(row['source_json'])
        saved = db.execute('SELECT * FROM pymatgen_inputs WHERE job_id=? ORDER BY position', (row['id'],)).fetchall()
        if len(sources) != len(originals):
            raise PymatgenError(409, '请求标识已用于不同的结构或工具参数。')
        for index, source in enumerate(sources):
            origin = {name: source[name] for name in ('type', 'filename') if name in source} if source['type'] == 'upload' else {
                'type': 'archive', 'job_id': source['job_id'], 'code': source['code']}
            if origin != originals[index]:
                raise PymatgenError(409, '请求标识已用于不同的结构或工具参数。')
            if source['type'] == 'upload':
                try:
                    encoded = source['content_base64']
                    if len(encoded) > (MAX_INPUT_BYTES + 2) // 3 * 4:
                        raise ValueError()
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error, TypeError):
                    raise PymatgenError(422, '结构文件编码或大小无效。') from None
                if sha256(content) != saved[index]['sha256']:
                    raise PymatgenError(409, '请求标识已用于不同的结构或工具参数。')
        return self.view(row)

    def enqueue(self, db, user_id, request_key, prepared):
        self.maintain(db)
        row = db.execute('SELECT * FROM pymatgen_jobs WHERE user_id=? AND request_key=?', (user_id, request_key)).fetchone()
        if row:
            if row['fingerprint'] != prepared['fingerprint']:
                raise PymatgenError(409, '请求标识已用于不同的结构或工具参数。')
            return self.view(row)
        if not self.enabled:
            raise PymatgenError(503, '晶体工具暂未开放，请稍后再试。')
        if db.execute("SELECT 1 FROM pymatgen_jobs WHERE user_id=? AND state IN ('queued','running')", (user_id,)).fetchone():
            raise PymatgenError(409, '已有排队或计算中的晶体工具任务，请完成或取消后再提交。')
        cost = COSTS[prepared['operation']]
        Credits.grant(db, user_id)
        if Credits.wallet(db, user_id)['available'] < cost:
            raise PymatgenError(402, f'可用积分不足，本次需要{cost}积分；成功后扣除，失败或取消退回。')
        identifier, now = uuid.uuid4().hex, time.time()
        db.execute('''INSERT INTO pymatgen_jobs(id,user_id,request_key,fingerprint,operation,state,
         parameters,source_json,input_name,created_at,updated_at,queue_deadline,reserved_credits,credit_status)
         VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,'frozen')''',
                   (identifier, user_id, request_key, prepared['fingerprint'], prepared['operation'],
                    dumps(prepared['parameters']), dumps(prepared['sources']),
                    '、'.join(item['filename'] for item in prepared['inputs']), now, now, now + QUEUE_SECONDS, cost))
        for index, item in enumerate(prepared['inputs']):
            db.execute('INSERT INTO pymatgen_inputs VALUES (?,?,?,?,?)',
                       (identifier, index, item['filename'], sha256(item['content']), item['content']))
        Credits.entry(db, user_id, 'pymatgen_freeze', -cost, cost, 'pymatgen:freeze:' + identifier, identifier)
        row = self.own(db, identifier, user_id)
        self.audit(db, row, 'queued', {'operation': row['operation'], 'reserved_credits': cost})
        return self.view(row)

    def status(self, db):
        ready = db.execute('SELECT 1 FROM pymatgen_workers WHERE last_seen>? AND available=1', (time.time() - 90,)).fetchone()
        return {'enabled': self.enabled, 'worker_online': bool(ready), 'limits': LIMITS, 'defaults': DEFAULT_PARAMETERS, 'costs': COSTS,
                'message': '' if self.enabled and ready else ('计算服务暂时不可用，任务将排队等待。' if self.enabled else '晶体工具暂未开放。')}

    def path(self, row):
        if not re.fullmatch(r'[0-9a-f]{32}', row['id']) or row['artifact_path'] != f"output/pymatgen/{row['id']}/result.zip":
            raise PymatgenError(410, '工具结果路径无法核验。')
        path = self.root / row['artifact_path']
        if (not path.resolve().is_relative_to(self.root)
                or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(self.root))):
            raise PymatgenError(410, '工具结果路径无法核验。')
        return path

    @staticmethod
    def inputs(db, row):
        values = db.execute('SELECT * FROM pymatgen_inputs WHERE job_id=? ORDER BY position', (row['id'],)).fetchall()
        if not values or any(item['content'] is None or sha256(bytes(item['content'])) != item['sha256'] for item in values):
            raise PymatgenError(410, '任务输入快照无法核验。')
        return [{'filename': item['filename'], 'content': bytes(item['content'])} for item in values]

    def read_bundle(self, row, *, db=None, validate=True):
        if row['state'] != 'succeeded' or not row['download_expires_at'] or row['download_expires_at'] <= time.time():
            raise PymatgenError(410, '工具结果尚未完成或已过期。')
        try:
            path = self.path(row)
            if path.stat().st_size > MAX_BUNDLE_BYTES:
                raise ValueError()
            data = path.read_bytes()
            if len(data) != row['artifact_size'] or sha256(data) != row['artifact_sha256']:
                raise ValueError()
            checked = validate_bundle(data, self.inputs(db, row), row['operation'], json.loads(row['parameters'])) if validate else None
            return data, checked
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            raise PymatgenError(410, '工具结果核验失败，已停止下载。') from None

    @staticmethod
    def message(row, available):
        refunded = f"冻结的{row['reserved_credits']}积分已退回。" if row['credit_status'] == 'refunded' else ''
        if row['state'] == 'succeeded':
            if row['file_expired'] or (row['download_expires_at'] and row['download_expires_at'] <= time.time()):
                return '工具结果已过期。'
            return '结果已核验，请在3天内下载。' if available else '工具结果核验失败，已停止下载，请联系管理员。'
        if row['cancel_requested'] and row['state'] == 'running':
            return '正在取消，请稍候。'
        if row['state'] == 'failed':
            return {'unsupported_occupancy': '当前仅支持满占有结构，部分占有或未知占有率暂不支持。',
                    'unsupported_disorder': '当前仅支持有序结构，无序结构暂不支持。',
                    'unsupported_structure': '结构无法用于当前工具，请核对晶胞、元素和对称操作。',
                    'unsupported_scale': '该结构超出当前小模型范围，请调整输入或工具参数。',
                    'resource': '本次计算资源不足，请稍后再试。'}.get(row['failure_code'], '计算未完成，请检查结构或参数后重试。') + refunded
        return {'queued': '已排队，结果将在本页显示。', 'running': '正在计算，请稍候。',
                'cancelled': '已取消。' + refunded, 'timed_out': '处理超时，请稍后重新提交。' + refunded}.get(row['state'], '请稍后查看任务状态。')

    def view(self, row):
        available = False
        if row['state'] == 'succeeded' and not row['file_expired']:
            try:
                self.read_bundle(row, validate=False)
                available = True
            except PymatgenError:
                pass
        return {'id': row['id'], 'kind': 'pymatgen', 'operation': row['operation'], 'state': row['state'],
                'phase': row['phase'], 'progress': row['progress'], 'input_name': row['input_name'],
                'parameters': json.loads(row['parameters']), 'created_at': iso(row['created_at']),
                'updated_at': iso(row['updated_at']), 'message': self.message(row, available),
                'credits': {'reserved': row['reserved_credits'], 'status': row['credit_status'],
                            'charged': row['reserved_credits'] if row['credit_status'] == 'charged' else 0},
                'cancel_requested': bool(row['cancel_requested']), 'download_available': available,
                'download_expires_at': iso(row['download_expires_at']),
                'file_expired': bool(row['file_expired'] or (row['download_expires_at'] and row['download_expires_at'] <= time.time())),
                'summary': json.loads(row['result_summary']) if row['result_summary'] else None}

    def settle(self, db, row, state, *, message='', completion_state=None):
        credit = row['credit_status']
        if credit == 'frozen':
            success, cost = state == 'succeeded', row['reserved_credits']
            Credits.entry(db, row['user_id'], 'pymatgen_charge' if success else 'pymatgen_refund',
                          0 if success else cost, -cost, 'pymatgen:settle:' + row['id'], row['id'])
            credit = 'charged' if success else 'refunded'
        now = time.time()
        db.execute('''UPDATE pymatgen_jobs SET state=?,phase=?,message=?,updated_at=?,finished_at=?,
         lease_until=NULL,completion_lease_hash=lease_hash,lease_hash=NULL,completion_state=?,progress=?,
         download_expires_at=?,credit_status=? WHERE id=?''',
                   (state, 'completed' if state == 'succeeded' else state, message, now, now, completion_state,
                    100 if state == 'succeeded' else row['progress'], now + RETENTION_SECONDS, credit, row['id']))
        self.audit(db, row, 'settled', {'state': state, 'credit_status': credit, 'detail': message})

    def maintain(self, db):
        now = time.time()
        rows = db.execute("SELECT * FROM pymatgen_jobs WHERE (state='queued' AND queue_deadline<=?) OR (state='running' AND (lease_until<=? OR run_deadline<=?))", (now, now, now)).fetchall()
        for row in rows:
            self.settle(db, row, 'cancelled' if row['cancel_requested'] else 'timed_out', message='队列或执行租约已超时。')
        for row in db.execute('SELECT * FROM pymatgen_jobs WHERE download_expires_at<=? AND file_expired=0', (now,)).fetchall():
            location = {'id': row['id'], 'artifact_path': f"output/pymatgen/{row['id']}/result.zip"}
            try:
                path = self.path(row if row['artifact_path'] else location)
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    path.parent.rmdir()
            except (PymatgenError, OSError):
                continue
            db.execute('UPDATE pymatgen_inputs SET content=NULL WHERE job_id=?', (row['id'],))
            db.execute('UPDATE pymatgen_jobs SET artifact_path=NULL,file_expired=1 WHERE id=?', (row['id'],))
            self.audit(db, row, 'files_expired')
        parent = self.root / 'output/pymatgen'
        if parent.exists() and not parent.is_symlink() and parent.resolve().is_relative_to(self.root):
            for directory in parent.glob('.pending-*'):
                if (re.fullmatch(r'\.pending-[0-9a-f]{32}-[A-Za-z0-9_-]+', directory.name)
                        and not directory.is_symlink() and directory.is_dir()
                        and directory.stat().st_mtime <= now - RETENTION_SECONDS):
                    path = directory / 'result.zip'
                    if not path.is_symlink():
                        path.unlink(missing_ok=True)
                        try:
                            directory.rmdir()
                        except OSError:
                            pass

    def cancel(self, db, job_id, user_id):
        self.maintain(db)
        row = self.own(db, job_id, user_id)
        if row['state'] == 'queued':
            db.execute('UPDATE pymatgen_jobs SET cancel_requested=1 WHERE id=?', (job_id,))
            self.settle(db, row, 'cancelled')
        elif row['state'] == 'running' and not row['cancel_requested']:
            db.execute('UPDATE pymatgen_jobs SET cancel_requested=1,updated_at=? WHERE id=?', (time.time(), job_id))
            self.audit(db, row, 'cancel_requested')
        return self.view(self.own(db, job_id, user_id))

    def claim(self, db, worker_id, available):
        science_queue.maintain(self, db)
        now = time.time()
        db.execute('INSERT INTO pymatgen_workers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,available=excluded.available', (worker_id, now, int(available)))
        if not self.enabled or not available or not science_queue.claimable(db, 'pymatgen'):
            return {'job': None}
        row = db.execute("SELECT * FROM pymatgen_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return {'job': None}
        inputs, token = self.inputs(db, row), new_token()
        db.execute("UPDATE pymatgen_jobs SET state='running',phase='preparing',started_at=?,updated_at=?,run_deadline=?,lease_until=?,worker_id=?,lease_hash=? WHERE id=?", (now, now, now + LEASE_SECONDS, now + LEASE_SECONDS, worker_id, digest(token), row['id']))
        self.audit(db, row, 'claimed', {'worker_id': worker_id})
        return {'job': {'id': row['id'], 'operation': row['operation'], 'parameters': json.loads(row['parameters']),
                        'inputs': [{'filename': item['filename'], 'content_base64': base64.b64encode(item['content']).decode('ascii'),
                                    'sha256': sha256(item['content'])} for item in inputs]},
                'lease_token': token, 'lease_seconds': LEASE_SECONDS}

    @staticmethod
    def leased(db, job_id, token):
        row = db.execute('SELECT * FROM pymatgen_jobs WHERE id=?', (job_id,)).fetchone()
        if (row is None or row['state'] != 'running' or not row['lease_hash']
                or not row['lease_until'] or row['lease_until'] <= time.time()
                or not row['run_deadline'] or row['run_deadline'] <= time.time()
                or not hmac.compare_digest(row['lease_hash'], digest(token))):
            raise PymatgenError(409, '工具任务租约已失效、已结束或已超时。')
        return row

    def heartbeat(self, db, job_id, token, progress=None, phase=None):
        self.maintain(db)
        row, now = self.leased(db, job_id, token), time.time()
        db.execute('UPDATE pymatgen_jobs SET lease_until=?,updated_at=?,progress=?,phase=? WHERE id=?',
                   (min(now + LEASE_SECONDS, row['run_deadline']), now, max(row['progress'], progress or 0), phase or row['phase'], job_id))
        db.execute('UPDATE pymatgen_workers SET last_seen=? WHERE id=?', (now, row['worker_id']))
        return {'accepted': True, 'cancel_requested': bool(row['cancel_requested'])}

    @staticmethod
    def canonical_zip(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type, entry.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
                archive.writestr(entry, files[name])
        return output.getvalue()

    def completed(self, db, job_id, token, state, encoded=None, prepared=None):
        row = db.execute('SELECT * FROM pymatgen_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row['state'] not in TERMINAL:
            return None
        if (not row['completion_lease_hash'] or not hmac.compare_digest(row['completion_lease_hash'], digest(token))
                or row['completion_state'] != state):
            raise PymatgenError(409, '工具任务已经结束，不能覆盖结果。')
        if state == 'succeeded':
            if prepared is not None:
                expected = prepared['sha256']
            else:
                if row['file_expired']:
                    raise PymatgenError(410, '工具结果已经过期。')
                try:
                    if not isinstance(encoded, str) or len(encoded) > (MAX_BUNDLE_BYTES + 2) // 3 * 4:
                        raise ValueError()
                    checked = validate_bundle(base64.b64decode(encoded, validate=True), self.inputs(db, row), row['operation'], json.loads(row['parameters']))
                    expected = sha256(self.canonical_zip(checked['files']))
                except (ValueError, TypeError, KeyError, OSError, binascii.Error):
                    raise PymatgenError(409, '已结束任务收到不同结果，已拒绝覆盖。') from None
            if expected != row['artifact_sha256']:
                raise PymatgenError(409, '已结束任务收到不同结果，已拒绝覆盖。')
        return {'accepted': True, 'state': row['state']}

    def prepare(self, db, row, encoded):
        try:
            if not isinstance(encoded, str) or len(encoded) > (MAX_BUNDLE_BYTES + 2) // 3 * 4:
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
            checked = validate_bundle(data, self.inputs(db, row), row['operation'], json.loads(row['parameters']))
            canonical = self.canonical_zip(checked['files'])
            if len(canonical) > MAX_BUNDLE_BYTES:
                raise ValueError()
        except (ValueError, KeyError, TypeError, binascii.Error, zipfile.BadZipFile):
            raise PymatgenError(422, '工具结果包复验失败，尚未发布结果。') from None
        destination = self.path({'id': row['id'], 'artifact_path': f"output/pymatgen/{row['id']}/result.zip"})
        destination.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent.parent, 0o700)
        temporary = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=f'.pending-{row["id"]}-', dir=destination.parent.parent))
            os.chmod(temporary, 0o700)
            path = temporary / 'result.zip'
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(canonical); stream.flush(); os.fsync(stream.fileno())
            return {'directory': temporary, 'sha256': sha256(canonical), 'size': len(canonical), 'summary': checked['summary']}
        except OSError:
            if temporary is not None:
                shutil.rmtree(temporary)
            raise PymatgenError(503, '工具结果暂时无法保存，请稍后重试。') from None

    def complete(self, db, job_id, token, state, prepared=None, message='', error_code=None):
        self.maintain(db)
        row = self.leased(db, job_id, token)
        if row['cancel_requested'] and state != 'cancelled':
            raise PymatgenError(409, '任务正在取消，请确认计算已经结束后提交取消结果。')
        if state == 'succeeded':
            if prepared is None:
                raise PymatgenError(422, '成功结果缺少完整文件包。')
            destination = self.path({'id': job_id, 'artifact_path': f'output/pymatgen/{job_id}/result.zip'})
            if destination.parent.exists():
                try:
                    if destination.stat().st_size != prepared['size'] or sha256(destination.read_bytes()) != prepared['sha256']:
                        raise OSError()
                except OSError:
                    raise PymatgenError(409, '已有结果与本次不一致，请管理员核查。') from None
            else:
                try:
                    prepared['directory'].rename(destination.parent)
                except OSError:
                    raise PymatgenError(503, '工具结果暂时无法发布，请稍后重试。') from None
            db.execute('UPDATE pymatgen_jobs SET artifact_path=?,artifact_sha256=?,artifact_size=?,result_summary=? WHERE id=?',
                       (destination.relative_to(self.root).as_posix(), prepared['sha256'], prepared['size'], dumps(prepared['summary']), job_id))
        db.execute('UPDATE pymatgen_jobs SET failure_code=? WHERE id=?', (error_code if state == 'failed' else None, job_id))
        self.settle(db, row, state, message=message, completion_state=state)
        return {'accepted': True, 'state': state}
