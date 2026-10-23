"""电镜模拟的独立队列；积分仍由全站 SQLite 原子账本结算。"""
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

from highscore_abtem.contract import (DEFAULT_PARAMETERS, LIMITS, MAX_CIF_BYTES,
                                      RETENTION_SECONDS, validate_cif_bytes,
                                      validate_parameters)
from highscore_abtem.artifacts import MAX_BUNDLE_BYTES
from highscore_downloader.archive import ArchiveError
from highscore_downloader.numbers import parse_codes
from .credits import Credits
from .database import dumps
from .security import digest, new_token
from . import science_queue

LEASE_SECONDS = 300
QUEUE_SECONDS = 86400
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS abtem_jobs (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),request_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL,state TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'queued',
 parameters TEXT NOT NULL,source_type TEXT NOT NULL,source_json TEXT NOT NULL,
 input_name TEXT NOT NULL,input_sha256 TEXT NOT NULL,input_cif BLOB,
 created_at REAL NOT NULL,updated_at REAL NOT NULL,queue_deadline REAL NOT NULL,
 started_at REAL,finished_at REAL,run_deadline REAL,download_expires_at REAL,
 credit_status TEXT NOT NULL DEFAULT 'frozen',progress INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0,file_expired INTEGER NOT NULL DEFAULT 0,
 worker_id TEXT,lease_hash TEXT,lease_until REAL,completion_lease_hash TEXT,
 completion_state TEXT,artifact_path TEXT,artifact_sha256 TEXT,artifact_size INTEGER,
 result_summary TEXT,message TEXT NOT NULL DEFAULT '',failure_code TEXT,UNIQUE(user_id,request_key)
);
CREATE INDEX IF NOT EXISTS abtem_queue ON abtem_jobs(state,created_at);
CREATE INDEX IF NOT EXISTS abtem_owner ON abtem_jobs(user_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS abtem_owner_active ON abtem_jobs(user_id)
 WHERE state IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS abtem_global_running ON abtem_jobs((1)) WHERE state='running';
CREATE TABLE IF NOT EXISTS abtem_workers (
 id TEXT PRIMARY KEY,last_seen REAL NOT NULL,available INTEGER NOT NULL
);
'''


class AbtemError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def sha256(value):
    return hashlib.sha256(value).hexdigest()


class AbtemStore:
    science_kind = 'abtem'

    def __init__(self, root, database, *, enabled=False):
        self.root = Path(root).resolve()
        self.database = database
        self.enabled = enabled is True
        with database.connection() as db:
            db.executescript(SCHEMA)
            if 'failure_code' not in {row['name'] for row in db.execute('PRAGMA table_info(abtem_jobs)')}:
                db.execute('ALTER TABLE abtem_jobs ADD COLUMN failure_code TEXT')
            science_queue.register(db, 'abtem', self.enabled)

    @staticmethod
    def audit(db, row, action, detail=None):
        db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)',
                   (row['user_id'], 'abtem_' + action,
                    dumps({'job_id': row['id'], **(detail or {})}), time.time()))

    @staticmethod
    def own(db, job_id, user_id):
        row = db.execute('SELECT * FROM abtem_jobs WHERE id=? AND user_id=?', (job_id, user_id)).fetchone()
        if row is None:
            raise AbtemError(404, '模拟任务不存在。')
        return row

    @staticmethod
    def expected(row):
        if row['artifact_expected']:
            return json.loads(row['artifact_expected'])
        payload = json.loads(row['payload'])
        return payload.get('selected') or [{'code': code.original} for code in parse_codes(payload['codes'])]

    def archive_input(self, db, user_id, job_id, code):
        from .source_files import selected_cif
        row = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=? AND kind='archive'",
                         (job_id, user_id)).fetchone()
        if row is None:
            raise AbtemError(404, '结构归档不存在。')
        if row['archive_status'] not in ('success', 'partial') or not row['artifact_dir']:
            raise AbtemError(409, '该任务尚无已核验的结构归档。')
        try:
            content = selected_cif(self.root, row['artifact_dir'], self.expected(row), code)
        except (ArchiveError, OSError, ValueError, KeyError, TypeError):
            raise AbtemError(409, '结构文件复验失败，请重新生成结构归档。') from None
        if content is None:
            raise AbtemError(404, '该编号没有已核验的 CIF 文件。')
        return content

    def input(self, db, user_id, source, parameters):
        try:
            params = validate_parameters(parameters)
            if source['type'] == 'upload':
                filename = source['filename']
                if (not filename.lower().endswith('.cif') or filename != filename.strip()
                        or any(char in filename for char in '/\\:')
                        or any(ord(char) < 32 for char in filename)):
                    raise ValueError('请上传有效的 CIF 文件名，不接受文件路径。')
                encoded = source['cif_base64']
                if len(encoded) > (MAX_CIF_BYTES + 2) // 3 * 4:
                    raise ValueError('请提供不超过512 KiB的CIF文件。')
                content = base64.b64decode(encoded, validate=True)
                origin = {'type': 'upload', 'filename': filename}
            else:
                code = source['code']
                if not re.fullmatch(r'ICSD-[0-9]{6}', code):
                    raise ValueError('请选择本人已生成的结构 CIF。')
                content = self.archive_input(db, user_id, source['job_id'], code)
                filename = code + '.cif'
                origin = {'type': 'archive', 'job_id': source['job_id'], 'code': code}
            validate_cif_bytes(content)
        except (ValueError, binascii.Error) as exc:
            if isinstance(exc, AbtemError):
                raise
            raise AbtemError(422, str(exc) or 'CIF 文件编码无效。') from None
        identity = {'parameters': params, 'source': origin, 'input_sha256': sha256(content)}
        return {'parameters': params, 'source': origin, 'content': content, 'name': filename,
                'fingerprint': sha256(dumps(identity).encode('utf-8'))}

    def sources(self, db, user_id, offset=0, limit=50):
        # selected_cif 同时复验原归档所有回执，候选列表不把磁盘文件名当作凭据。
        from .artifacts import validate_directory
        result = []
        rows = db.execute("SELECT * FROM jobs WHERE user_id=? AND kind='archive' AND archive_status IN ('success','partial') AND artifact_dir IS NOT NULL ORDER BY created_at DESC,id DESC", (user_id,)).fetchall()
        for row in rows:
            try:
                receipts = validate_directory(self.root / row['artifact_dir'], self.expected(row))
                for receipt in receipts:
                    if receipt.get('export_method') != 'jade_structure_readonly_report' or 'cif' not in receipt['files']:
                        continue
                    self.archive_input(db, user_id, row['id'], receipt['code'])
                    result.append({'job_id': row['id'], 'code': receipt['code'],
                                   'phase_name': receipt['phase_name'], 'source': receipt['source']})
            except (AbtemError, ArchiveError, ValueError, OSError, KeyError, TypeError):
                continue
        return {'items': result[offset:offset + limit],
                'next_offset': offset + limit if len(result) > offset + limit else None}

    def replay(self, db, user_id, request_key, source, parameters):
        """先核对已接受的输入身份；未知提交重放不重新依赖原归档文件。"""
        row = db.execute('SELECT * FROM abtem_jobs WHERE user_id=? AND request_key=?',
                         (user_id, request_key)).fetchone()
        if row is None:
            return None
        try:
            params = validate_parameters(parameters)
        except ValueError as exc:
            raise AbtemError(422, str(exc)) from None
        if params != json.loads(row['parameters']) or source['type'] != row['source_type']:
            raise AbtemError(409, '请求标识已用于不同的输入或模拟参数。')
        if source['type'] == 'archive':
            origin = {'type': 'archive', 'job_id': source['job_id'], 'code': source['code']}
            if origin != json.loads(row['source_json']):
                raise AbtemError(409, '请求标识已用于不同的输入或模拟参数。')
        else:
            origin = {'type': 'upload', 'filename': source['filename']}
            if origin != json.loads(row['source_json']):
                raise AbtemError(409, '请求标识已用于不同的输入或模拟参数。')
            try:
                encoded = source['cif_base64']
                if len(encoded) > (MAX_CIF_BYTES + 2) // 3 * 4:
                    raise ValueError()
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise AbtemError(422, 'CIF 文件编码或大小无效。') from None
            if sha256(content) != row['input_sha256']:
                raise AbtemError(409, '请求标识已用于不同的输入或模拟参数。')
        return self.view(row)

    def enqueue(self, db, user_id, request_key, prepared):
        self.maintain(db)
        row = db.execute('SELECT * FROM abtem_jobs WHERE user_id=? AND request_key=?',
                         (user_id, request_key)).fetchone()
        if row:
            if row['fingerprint'] != prepared['fingerprint']:
                raise AbtemError(409, '请求标识已用于不同的输入或模拟参数。')
            return self.view(row)
        if not self.enabled:
            raise AbtemError(503, '电镜模拟暂未开放，请稍后再试。')
        if db.execute("SELECT 1 FROM abtem_jobs WHERE user_id=? AND state IN ('queued','running')", (user_id,)).fetchone():
            raise AbtemError(409, '已有排队或计算中的模拟任务，请完成或取消后再提交。')
        Credits.grant(db, user_id)
        if Credits.wallet(db, user_id)['available'] < 1:
            raise AbtemError(402, '可用积分不足1分，可每日签到获得积分。')
        identifier, now = uuid.uuid4().hex, time.time()
        db.execute('''INSERT INTO abtem_jobs(id,user_id,request_key,fingerprint,state,parameters,
          source_type,source_json,input_name,input_sha256,input_cif,created_at,updated_at,queue_deadline)
          VALUES (?,?,?,?,'queued',?,?,?,?,?,?,?,?,?)''',
                   (identifier, user_id, request_key, prepared['fingerprint'], dumps(prepared['parameters']),
                    prepared['source']['type'], dumps(prepared['source']), prepared['name'],
                    sha256(prepared['content']), prepared['content'], now, now, now + QUEUE_SECONDS))
        Credits.entry(db, user_id, 'abtem_freeze', -1, 1, 'abtem:freeze:' + identifier, identifier)
        row = self.own(db, identifier, user_id)
        self.audit(db, row, 'queued', {'input_sha256': row['input_sha256']})
        return self.view(row)

    def status(self, db):
        ready = db.execute('SELECT 1 FROM abtem_workers WHERE last_seen>? AND available=1',
                           (time.time() - 90,)).fetchone()
        return {'enabled': self.enabled, 'worker_online': bool(ready),
                'message': '' if self.enabled and ready else ('模拟服务暂时不可用，任务将排队等待。' if self.enabled else '电镜模拟暂未开放。'),
                'limits': LIMITS, 'defaults': DEFAULT_PARAMETERS}

    def path(self, row):
        if (not re.fullmatch(r'[0-9a-f]{32}', row['id'])
                or row['artifact_path'] != f"output/abtem/{row['id']}/result.zip"):
            raise AbtemError(410, '模拟结果路径无法核验。')
        path = self.root / row['artifact_path']
        if (any(part.is_symlink() for part in [path, *path.parents] if part.is_relative_to(self.root))
                or not path.resolve().is_relative_to(self.root)):
            raise AbtemError(410, '模拟结果路径无法核验。')
        return path

    def read_bundle(self, row, *, validate=True):
        if row['state'] != 'succeeded' or not row['download_expires_at'] or row['download_expires_at'] <= time.time():
            raise AbtemError(410, '模拟结果尚未完成或已过期。')
        try:
            path = self.path(row)
            if path.stat().st_size > MAX_BUNDLE_BYTES:
                raise AbtemError(410, '模拟结果核验失败，已停止下载。')
            data = path.read_bytes()
            if len(data) != row['artifact_size'] or sha256(data) != row['artifact_sha256']:
                raise AbtemError(410, '模拟结果核验失败，已停止下载。')
            checked = None
            if validate:
                from highscore_abtem.artifacts import validate_bundle
                if row['input_cif'] is None:
                    raise ValueError('输入快照缺失。')
                checked = validate_bundle(data, bytes(row['input_cif']), json.loads(row['parameters']))
            return data, checked
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            raise AbtemError(410, '模拟结果核验失败，已停止下载。') from None

    @staticmethod
    def message(row, available):
        if row['state'] == 'succeeded':
            if row['file_expired'] or (row['download_expires_at'] and row['download_expires_at'] <= time.time()):
                return '模拟结果已过期。'
            return '模拟结果已核验，已扣1积分；请在3天内下载。' if available else '模拟结果核验失败，已停止下载，请联系管理员。'
        if row['cancel_requested'] and row['state'] == 'running':
            return '正在取消模拟，结束后退回冻结积分。'
        if row['state'] == 'failed':
            reasons = {
                'unsupported_occupancy': '当前仅支持满占有结构，部分占有或未知占有率暂不支持。',
                'unsupported_disorder': '当前仅支持有序结构，无序结构暂不支持。',
                'unsupported_structure': '该 CIF 结构无法用于当前模拟，请核对晶胞、元素和对称操作。',
                'unsupported_scale': '该结构或方向超出当前小模型范围，请调整输入或模拟参数。',
                'unsupported_precision': '本次模型未达到模拟精度要求，请调整输入或模拟参数。',
                'resource': '本次模拟资源不足，请稍后再试。',
            }
            reason = reasons.get(row['failure_code'])
            if reason:
                return reason + '冻结积分已退回。'
        return {'queued': '已排队，冻结1积分；两项结果成功后合计扣1分。',
                'running': '正在计算模拟结果，请稍候。',
                'failed': '模拟未完成，冻结积分已退回。',
                'cancelled': '已取消，冻结积分已退回。',
                'timed_out': '模拟处理超时，冻结积分已退回。'}.get(row['state'], '请稍后查看模拟状态。')

    def view(self, row):
        available = False
        if row['state'] == 'succeeded' and not row['file_expired']:
            try:
                self.read_bundle(row, validate=False)
                available = True
            except AbtemError:
                pass
        return {'id': row['id'], 'kind': 'abtem', 'state': row['state'], 'phase': row['phase'],
                'message': self.message(row, available), 'created_at': iso(row['created_at']),
                'updated_at': iso(row['updated_at']), 'progress': row['progress'],
                'parameters': json.loads(row['parameters']), 'input_name': row['input_name'],
                'source_type': row['source_type'], 'credits': {'reserved': 1,
                    'charged': 1 if row['credit_status'] == 'charged' else 0, 'status': row['credit_status']},
                'download_available': available, 'download_expires_at': iso(row['download_expires_at']),
                'file_expired': bool(row['file_expired'] or (row['download_expires_at'] and row['download_expires_at'] <= time.time())),
                'cancel_requested': bool(row['cancel_requested']),
                'result_summary': json.loads(row['result_summary']) if row['result_summary'] else None}

    def settle(self, db, row, state, *, message='', completion_state=None):
        if row['credit_status'] == 'frozen':
            success = state == 'succeeded'
            Credits.entry(db, row['user_id'], 'abtem_charge' if success else 'abtem_refund',
                          0 if success else 1, -1, 'abtem:settle:' + row['id'], row['id'])
            credit = 'charged' if success else 'refunded'
        else:
            credit = row['credit_status']
        now = time.time()
        db.execute('''UPDATE abtem_jobs SET state=?,phase=?,message=?,credit_status=?,updated_at=?,
           finished_at=?,lease_until=NULL,completion_lease_hash=lease_hash,lease_hash=NULL,
           completion_state=?,progress=?,download_expires_at=? WHERE id=?''',
                   (state, 'completed' if state == 'succeeded' else state, message, credit, now, now,
                    completion_state, 100 if state == 'succeeded' else row['progress'],
                    now + RETENTION_SECONDS, row['id']))
        self.audit(db, row, 'settled', {'state': state, 'credit_status': credit, 'detail': message})

    def maintain(self, db):
        now = time.time()
        rows = db.execute("SELECT * FROM abtem_jobs WHERE (state='queued' AND queue_deadline<=?) OR (state='running' AND (lease_until<=? OR run_deadline<=?))", (now, now, now)).fetchall()
        for row in rows:
            self.settle(db, row, 'cancelled' if row['cancel_requested'] else 'timed_out', message='队列或执行租约已超时。')
        for row in db.execute('SELECT * FROM abtem_jobs WHERE state IN (\'succeeded\',\'failed\',\'cancelled\',\'timed_out\') AND download_expires_at<=? AND file_expired=0', (now,)).fetchall():
            # 包括文件发布后、SQLite提交前退出留下的同任务孤立结果。
            candidate = row if row['artifact_path'] else {
                'id': row['id'], 'artifact_path': f"output/abtem/{row['id']}/result.zip"}
            if row['artifact_path'] or (self.root / candidate['artifact_path']).parent.exists():
                try:
                    path = self.path(candidate)
                    path.unlink(missing_ok=True)
                    if path.parent.exists():
                        path.parent.rmdir()
                except (AbtemError, OSError):
                    # 不能越过异常路径清理，也不能把清理失败记成已清理。
                    continue
            db.execute('UPDATE abtem_jobs SET input_cif=NULL,artifact_path=NULL,file_expired=1 WHERE id=?', (row['id'],))
            self.audit(db, row, 'files_expired')
        parent = self.root / 'output/abtem'
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
            db.execute('UPDATE abtem_jobs SET cancel_requested=1 WHERE id=?', (job_id,))
            self.settle(db, row, 'cancelled')
        elif row['state'] == 'running' and not row['cancel_requested']:
            db.execute('UPDATE abtem_jobs SET cancel_requested=1,updated_at=? WHERE id=?', (time.time(), job_id))
            self.audit(db, row, 'cancel_requested')
        return self.view(self.own(db, job_id, user_id))

    def claim(self, db, worker_id, available):
        science_queue.maintain(self, db)
        now = time.time()
        db.execute('INSERT INTO abtem_workers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,available=excluded.available', (worker_id, now, int(available)))
        if not self.enabled or not available or not science_queue.claimable(db, 'abtem'):
            return {'job': None}
        row = db.execute("SELECT * FROM abtem_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return {'job': None}
        token = new_token()
        db.execute("UPDATE abtem_jobs SET state='running',phase='preparing',started_at=?,updated_at=?,run_deadline=?,lease_until=?,worker_id=?,lease_hash=? WHERE id=?", (now, now, now + LEASE_SECONDS, now + LEASE_SECONDS, worker_id, digest(token), row['id']))
        self.audit(db, row, 'claimed', {'worker_id': worker_id})
        return {'job': {'id': row['id'], 'parameters': json.loads(row['parameters']),
                        'cif_base64': base64.b64encode(bytes(row['input_cif'])).decode('ascii'),
                        'input_sha256': row['input_sha256'], 'input_name': row['input_name']},
                'lease_token': token, 'lease_seconds': LEASE_SECONDS}

    @staticmethod
    def leased(db, job_id, token):
        row = db.execute('SELECT * FROM abtem_jobs WHERE id=?', (job_id,)).fetchone()
        if (row is None or row['state'] != 'running' or not row['lease_hash']
                or not row['lease_until'] or row['lease_until'] <= time.time()
                or not row['run_deadline'] or row['run_deadline'] <= time.time()
                or not hmac.compare_digest(row['lease_hash'], digest(token))):
            raise AbtemError(409, '模拟任务租约已失效、已结束或已超时。')
        return row

    def heartbeat(self, db, job_id, token, progress=None, phase=None):
        self.maintain(db)
        row = self.leased(db, job_id, token)
        now = time.time()
        db.execute('UPDATE abtem_jobs SET lease_until=?,updated_at=?,progress=?,phase=? WHERE id=?',
                   (min(now + LEASE_SECONDS, row['run_deadline']), now,
                    max(row['progress'], progress or 0), phase or row['phase'], job_id))
        db.execute('UPDATE abtem_workers SET last_seen=? WHERE id=?', (now, row['worker_id']))
        return {'accepted': True, 'cancel_requested': bool(row['cancel_requested'])}

    def completed(self, db, job_id, token, state, encoded=None, prepared=None):
        row = db.execute('SELECT * FROM abtem_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row['state'] not in TERMINAL:
            return None
        if (not row['completion_lease_hash'] or not hmac.compare_digest(row['completion_lease_hash'], digest(token))
                or row['completion_state'] != state):
            raise AbtemError(409, '模拟任务已经结束，不能覆盖结果。')
        if state == 'succeeded':
            if prepared is not None:
                if prepared['sha256'] != row['artifact_sha256']:
                    raise AbtemError(409, '已结束任务收到不同结果，已拒绝覆盖。')
                return {'accepted': True, 'state': row['state']}
            try:
                original = base64.b64decode(encoded or '', validate=True)
            except (ValueError, binascii.Error):
                raise AbtemError(422, '结果包编码无效。') from None
            # 比较科学文件的规范ZIP，避免仅因zip压缩元数据不同重复结算。
            if row['file_expired']:
                raise AbtemError(410, '模拟结果已经过期。')
            from highscore_abtem.artifacts import validate_bundle
            try:
                checked = validate_bundle(original, bytes(row['input_cif']), json.loads(row['parameters']))
                if sha256(self.canonical_zip(checked['files'])) != row['artifact_sha256']:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, OSError):
                raise AbtemError(409, '已结束任务收到不同结果，已拒绝覆盖。') from None
        return {'accepted': True, 'state': row['state']}

    @staticmethod
    def canonical_zip(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o600 << 16
                archive.writestr(entry, files[name])
        return output.getvalue()

    def prepare(self, row, encoded):
        try:
            if not isinstance(encoded, str) or len(encoded) > (MAX_BUNDLE_BYTES + 2) // 3 * 4:
                raise ValueError()
            content = base64.b64decode(encoded, validate=True)
            if len(content) > MAX_BUNDLE_BYTES:
                raise ValueError()
            from highscore_abtem.artifacts import validate_bundle
            checked = validate_bundle(content, bytes(row['input_cif']), json.loads(row['parameters']))
            canonical = self.canonical_zip(checked['files'])
            if len(canonical) > MAX_BUNDLE_BYTES:
                raise ValueError()
        except (ValueError, KeyError, TypeError, binascii.Error, zipfile.BadZipFile):
            raise AbtemError(422, '模拟结果包复验失败，未发布结果或扣分。') from None
        location = {'id': row['id'], 'artifact_path': f"output/abtem/{row['id']}/result.zip"}
        destination = self.path(location)
        destination.parent.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(destination.parent.parent, 0o700)
        temporary = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=f'.pending-{row["id"]}-', dir=destination.parent.parent))
            os.chmod(temporary, 0o700)
            path = temporary / 'result.zip'
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(canonical)
                stream.flush()
                os.fsync(stream.fileno())
            return {'directory': temporary, 'path': path, 'sha256': sha256(canonical),
                    'size': len(canonical), 'metadata': checked['metadata']}
        except OSError:
            if temporary is not None:
                shutil.rmtree(temporary)
            raise AbtemError(503, '模拟结果暂时无法保存，请稍后重试。') from None

    def complete(self, db, job_id, token, state, prepared=None, message='', error_code=None):
        self.maintain(db)
        row = self.leased(db, job_id, token)
        if row['cancel_requested'] and state != 'cancelled':
            raise AbtemError(409, '任务正在取消，请确认计算已经结束后提交取消结果。')
        if state == 'succeeded':
            if prepared is None:
                raise AbtemError(422, '成功结果缺少完整文件包。')
            destination = self.path({'id': job_id, 'artifact_path': f'output/abtem/{job_id}/result.zip'})
            if destination.parent.exists():
                # 文件发布成功、数据库提交前退出，可仅接续相同内容，禁止覆盖未知结果。
                try:
                    if destination.stat().st_size != prepared['size'] or sha256(destination.read_bytes()) != prepared['sha256']:
                        raise OSError()
                except OSError:
                    raise AbtemError(409, '已有结果与本次不一致，请管理员核查。') from None
            else:
                try:
                    prepared['directory'].rename(destination.parent)
                except OSError:
                    raise AbtemError(503, '模拟结果暂时无法发布，请稍后重试。') from None
            db.execute('UPDATE abtem_jobs SET artifact_path=?,artifact_sha256=?,artifact_size=?,result_summary=? WHERE id=?',
                       (destination.relative_to(self.root).as_posix(), prepared['sha256'], prepared['size'],
                        dumps(self.summary(prepared['metadata'])), job_id))
        db.execute('UPDATE abtem_jobs SET failure_code=? WHERE id=?',
                   (error_code if state == 'failed' else None, job_id))
        self.settle(db, row, state, message=message, completion_state=state)
        return {'accepted': True, 'state': state}

    @staticmethod
    def summary(metadata):
        # 白名单摘要；执行器诊断、路径和完整版本/模型属性只保留在结果回执。
        result = {}
        for key in ('atom_count', 'grid', 'actual_thickness_nm', 'elapsed_seconds'):
            if key in metadata:
                result[key] = metadata[key]
        for section in ('model', 'runtime'):
            value = metadata.get(section)
            if isinstance(value, dict):
                for key in ('atom_count', 'grid', 'actual_thickness_nm', 'elapsed_seconds'):
                    if key in value:
                        result[key] = value[key]
        if isinstance(metadata.get('model'), dict):
            model = metadata['model']
            if isinstance(model.get('symbols'), list):
                result['atom_count'] = len(model['symbols'])
            if isinstance(model.get('cell_angstrom'), list) and len(model['cell_angstrom']) == 3:
                result['actual_thickness_nm'] = model['cell_angstrom'][2] / 10
        if isinstance(metadata.get('runtime'), dict) and 'seconds' in metadata['runtime']:
            result['elapsed_seconds'] = metadata['runtime']['seconds']
        if isinstance(metadata.get('coordinates'), dict):
            hrtem = metadata['coordinates'].get('hrtem')
            if isinstance(hrtem, dict) and isinstance(hrtem.get('shape'), list):
                result['grid'] = hrtem['shape']
        return result
