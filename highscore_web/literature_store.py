"""文献任务与积分账本：短写事务、独立执行锁和可核对的结算。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import tempfile
import uuid

from .database import dumps
from .credits import Credits
from .debug_accounts import is_debug
from highscore_literature.pdf import normalize_title, title_matches, validate_pdf

SCHEMA = '''
CREATE TABLE IF NOT EXISTS literature_jobs (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),title TEXT NOT NULL,title_key TEXT NOT NULL,
 canonical_title TEXT,doi TEXT,state TEXT NOT NULL,message TEXT NOT NULL,
 created_at REAL NOT NULL,deadline REAL NOT NULL,ready_at REAL,download_expires_at REAL,
 artifact_path TEXT,artifact_sha256 TEXT,artifact_size INTEGER,credit_status TEXT NOT NULL,
 candidates TEXT NOT NULL DEFAULT '[]',external_request_id TEXT,account_ref TEXT,
 lease_hash TEXT,lease_until REAL,worker_id TEXT,next_poll REAL NOT NULL DEFAULT 0,
 file_expired INTEGER NOT NULL DEFAULT 0,selected_candidate TEXT,
 source_revision INTEGER NOT NULL DEFAULT 0,stage TEXT NOT NULL DEFAULT '',last_checked_at REAL
);
CREATE INDEX IF NOT EXISTS literature_owner ON literature_jobs(user_id,created_at);
CREATE INDEX IF NOT EXISTS literature_queue ON literature_jobs(state,next_poll);
CREATE TABLE IF NOT EXISTS literature_request_keys (
 user_id TEXT NOT NULL REFERENCES users(id),request_key TEXT NOT NULL,title_key TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES literature_jobs(id),PRIMARY KEY(user_id,request_key)
);
CREATE TABLE IF NOT EXISTS literature_workers (
 id TEXT PRIMARY KEY,last_seen REAL NOT NULL,accounts_ready INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS literature_reminders (
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE REFERENCES literature_jobs(id),
 state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
 lease_hash TEXT,lease_until REAL,worker_id TEXT,submitted_at REAL,attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS literature_reminder_queue ON literature_reminders(state,created_at);
'''
ACTIVE = ('queued','running','waiting','needs_selection','needs_attention')


class LiteratureError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


class LiteratureStore:
    def __init__(self, root, database):
        self.root, self.database = Path(root), database
        with database.connection(write=True) as db:
            db.executescript(SCHEMA)
            job_columns = {row['name'] for row in db.execute('PRAGMA table_info(literature_jobs)')}
            if 'selected_candidate' not in job_columns:
                db.execute('ALTER TABLE literature_jobs ADD COLUMN selected_candidate TEXT')
            for name, definition in (('source_revision', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('stage', "TEXT NOT NULL DEFAULT ''"), ('last_checked_at', 'REAL')):
                if name not in job_columns:
                    db.execute(f'ALTER TABLE literature_jobs ADD COLUMN {name} {definition}')
            reminder_columns={row['name'] for row in db.execute('PRAGMA table_info(literature_reminders)')}
            if 'attempts' not in reminder_columns:
                db.execute('ALTER TABLE literature_reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
            if 'next_attempt' not in reminder_columns:
                db.execute('ALTER TABLE literature_reminders ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0')
            db.execute("INSERT OR IGNORE INTO settings VALUES ('literature_enabled','false')")
            for user in db.execute('SELECT id FROM users WHERE verified=1').fetchall():
                self.grant(db, user['id'])

    entry=staticmethod(Credits.entry)
    grant=staticmethod(Credits.grant)
    today=staticmethod(Credits.today)
    wallet=staticmethod(Credits.wallet)

    def settle(self, db, row, state, message):
        if row['credit_status'] == 'frozen':
            kind = 'charge' if state == 'succeeded' else 'refund'
            self.entry(db,row['user_id'],kind,0 if kind=='charge' else 1,-1,'settle:'+row['id'],row['id'])
            credit_status = 'charged' if kind=='charge' else 'refunded'
        else:
            credit_status = row['credit_status']
        db.execute('UPDATE literature_jobs SET state=?,message=?,credit_status=?,lease_hash=NULL,lease_until=NULL WHERE id=?',
                   (state,message,credit_status,row['id']))

    def artifact(self, row):
        relative = row['artifact_path']
        if not relative or relative != f"output/literature/{row['id']}/fulltext.pdf" or not re.fullmatch(r'[a-f0-9]{32}', row['id']):
            raise LiteratureError(410,'全文文件不可用，请联系管理员核查。')
        path = self.root / relative
        if any(parent.is_symlink() for parent in [path,*path.parents] if parent.is_relative_to(self.root)) or not path.resolve().is_relative_to(self.root.resolve()):
            raise LiteratureError(410,'全文路径无法核验。')
        return path

    def read_pdf(self, row):
        if row['state'] != 'succeeded' or not row['download_expires_at'] or row['download_expires_at'] <= time.time():
            raise LiteratureError(410,'文件已过期或尚未取得，请查看任务状态。')
        try:
            data = self.artifact(row).read_bytes()
        except OSError:
            raise LiteratureError(410,'全文文件不可用，请联系管理员核查。') from None
        if len(data) != row['artifact_size'] or hashlib.sha256(data).hexdigest() != row['artifact_sha256']:
            raise LiteratureError(410,'全文文件校验失败，已停止下载。')
        return data

    def maintain(self, db):
        now = time.time()
        for row in db.execute("SELECT * FROM literature_jobs WHERE state IN ('queued','running','waiting','needs_selection','needs_attention') AND deadline<=?", (now,)).fetchall():
            self.settle(db,row,'failed','求助已超过24小时，冻结积分已退回。')
        for row in db.execute('SELECT * FROM literature_jobs WHERE download_expires_at<=? AND artifact_path IS NOT NULL', (now,)).fetchall():
            try:
                self.artifact(row).unlink(missing_ok=True)
            except (OSError,LiteratureError):
                continue
            db.execute('UPDATE literature_jobs SET artifact_path=NULL,file_expired=1 WHERE id=?', (row['id'],))
        db.execute("UPDATE literature_jobs SET state='needs_attention',message='执行器中断，正在核查原求助；不会盲目重复发布',lease_hash=NULL,lease_until=NULL,next_poll=? WHERE lease_until<=? AND state='running'", (now+15,now))
        db.execute("UPDATE literature_reminders SET state='unknown',message='邮件提交状态待核查；为避免重复发送，已停止自动重发',lease_hash=NULL,lease_until=NULL,updated_at=? WHERE state='sending' AND lease_until<=?", (now,now))

    def queue_reminder(self, db, row):
        """在全文成功结算的同一事务内创建一封独立提醒。"""
        if is_debug(db, row['user_id']):
            return
        if row['state'] not in {'running','succeeded'}:
            raise LiteratureError(409, '文献任务状态不允许创建下载提醒。')
        user = db.execute('SELECT email,verified FROM users WHERE id=?', (row['user_id'],)).fetchone()
        state = 'queued' if user and user['verified'] and user['email'] else 'failed'
        message = '' if state == 'queued' else '未找到已验证的邮箱地址，提醒未发送。'
        now = time.time()
        db.execute('INSERT OR IGNORE INTO literature_reminders(id,job_id,state,created_at,updated_at,message) VALUES (?,?,?,?,?,?)',
                   (uuid.uuid4().hex,row['id'],state,now,now,message))

    def queue_existing_success_reminder(self, db, job_id):
        """显式补发已成功旧任务的提醒；按任务唯一键防止重复排队。"""
        row = db.execute('SELECT * FROM literature_jobs WHERE id=?',(job_id,)).fetchone()
        if row is None or row['state']!='succeeded':
            raise LiteratureError(409,'仅可为已成功的文献任务补建下载提醒。')
        if not row['download_expires_at'] or row['download_expires_at']<=time.time():
            raise LiteratureError(410,'全文已过期，不能创建下载提醒。')
        self.read_pdf(row)
        existing=db.execute('SELECT id,state FROM literature_reminders WHERE job_id=?',(job_id,)).fetchone()
        if existing:
            return {'id':existing['id'],'state':existing['state'],'created':False}
        if is_debug(db, row['user_id']):
            return {'id':None,'state':'not_requested','created':False}
        self.queue_reminder(db,row)
        added=db.execute('SELECT id,state FROM literature_reminders WHERE job_id=?',(job_id,)).fetchone()
        return {'id':added['id'],'state':added['state'],'created':True}

    def claim_reminder(self, db, worker_id):
        """原子领取一封提醒；租约过期后标成 unknown，绝不自动重复发送。"""
        self.maintain(db)
        while True:
            row = db.execute('''SELECT r.*,j.user_id,j.title,j.download_expires_at,j.state AS job_state,
                       u.email,u.verified,j.artifact_path,j.artifact_sha256,j.artifact_size,j.id AS task_id
                FROM literature_reminders r JOIN literature_jobs j ON j.id=r.job_id
                JOIN users u ON u.id=j.user_id WHERE r.state='queued' AND r.next_attempt<=?
                AND NOT EXISTS (SELECT 1 FROM debug_accounts d WHERE d.user_id=u.id)
                ORDER BY r.created_at LIMIT 1''',
                (time.time(),)).fetchone()
            if row is None:
                return {'reminder': None}
            try:
                if (row['job_state'] != 'succeeded' or not row['verified'] or
                        not row['download_expires_at'] or row['download_expires_at'] <= time.time()):
                    raise LiteratureError(410, '全文已过期或不可用。')
                self.read_pdf({'id':row['task_id'],'artifact_path':row['artifact_path'],
                               'state':row['job_state'],'download_expires_at':row['download_expires_at'],
                               'artifact_size':row['artifact_size'],'artifact_sha256':row['artifact_sha256']})
                from highscore_downloader.mail_settings import validate_email
                recipient = validate_email(row['email'])
            except (OSError, LiteratureError, ValueError, TypeError):
                db.execute("UPDATE literature_reminders SET state='failed',message='全文或邮箱核验失败，提醒未发送',updated_at=? WHERE id=? AND state='queued'",
                           (time.time(),row['id']))
                continue
            from .security import new_token, digest
            token = new_token()
            now = time.time()
            db.execute("UPDATE literature_reminders SET state='sending',worker_id=?,lease_hash=?,lease_until=?,attempts=attempts+1,updated_at=?,message='正在发送下载提醒' WHERE id=? AND state='queued'",
                       (worker_id,digest(token),now+180,now,row['id']))
            return {'reminder': {'id':row['id'],'task_id':row['task_id'],'title':row['title'],
                                 'recipient':recipient,'download_expires_at':row['download_expires_at'],
                                 'attempt':row['attempts']+1},
                    'lease_token':token,'lease_seconds':180}

    def complete_reminder(self, db, reminder_id, lease_token, state):
        import hmac
        from .security import digest
        row = db.execute('SELECT * FROM literature_reminders WHERE id=?', (reminder_id,)).fetchone()
        if row is None:
            raise LiteratureError(404, '下载提醒任务不存在。')
        if row['state'] in {'submitted','failed','unknown'}:
            if row['state'] == state:
                return {'accepted': True, 'state': row['state']}
            raise LiteratureError(409, '下载提醒已结束，状态不能覆盖。')
        if (row['state'] != 'sending' or not row['lease_hash'] or not row['lease_until']
                or row['lease_until'] <= time.time()
                or not hmac.compare_digest(row['lease_hash'],digest(lease_token))):
            raise LiteratureError(409, '下载提醒租约已失效。')
        now = time.time()
        if state=='failed' and row['attempts']<3:
            db.execute("UPDATE literature_reminders SET state='queued',message='邮件未获 SMTP 接受，将稍后自动重试',next_attempt=?,lease_hash=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                       (now+30*row['attempts'],now,reminder_id))
            return {'accepted': True, 'state': 'queued'}
        message = {'submitted':'SMTP 已接受下载提醒。','failed':'下载提醒发送失败。',
                   'unknown':'邮件提交状态待核查，已停止自动重发。'}[state]
        db.execute('UPDATE literature_reminders SET state=?,message=?,submitted_at=?,lease_hash=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                   (state,message,now if state=='submitted' else None,now,reminder_id))
        return {'accepted': True, 'state': state}

    @staticmethod
    def own(db, job_id, user_id):
        row = db.execute('SELECT * FROM literature_jobs WHERE id=? AND user_id=?', (job_id,user_id)).fetchone()
        if row is None:
            raise LiteratureError(404,'文献任务不存在。')
        return row

    @staticmethod
    def public_message(row):
        # 后台说明可能包含平台、账号或操作细节；新旧任务均只按业务状态对外展示。
        state = row['state']
        if state == 'succeeded':
            if row['file_expired'] or (row['download_expires_at'] and row['download_expires_at'] <= time.time()):
                return '全文已过期，请重新提交求助。'
            if row['stage'] == 'ready':
                return '可下载'
            return ('全文已核验，已扣1积分；请在3天内下载。' if row['credit_status'] == 'charged'
                    else '全文已核验，可在有效期内下载。')
        if state == 'failed':
            if row['message'] == '求助已超过24小时，冻结积分已退回。' and row['credit_status'] == 'refunded':
                return '求助已超过24小时，冻结积分已退回。'
            return '文献求助未完成，冻结积分已退回。' if row['credit_status'] == 'refunded' else '文献求助未完成，请重新提交或联系管理员。'
        if state == 'cancelled':
            return '已取消，冻结积分已退回。' if row['credit_status'] == 'refunded' else '文献求助已取消。'
        if state in ('queued','running','waiting'):
            staged = {'fetching':'正在获取正文','awaiting_fulltext':'等待全文',
                      'renewing':'正在重新获取','verifying':'正在核验','retrying':'稍后重试'}
            if row['stage'] in staged:
                return staged[row['stage']]
        return {
            'queued': '已排队，正在等待处理。',
            'running': '正在处理文献求助，请稍候。',
            'waiting': '正在检索文献并等待全文，请稍后查看。',
            'needs_selection': '找到多个匹配结果，请确认需要的论文。',
            'needs_attention': '文献暂时无法获取，请稍后查看或联系管理员。',
        }.get(state, '任务状态待确认，请稍后查看。')

    def view(self, row):
        available = False
        message = self.public_message(row)
        if row['state']=='succeeded' and row['download_expires_at'] and row['download_expires_at']>time.time() and row['artifact_path']:
            try:
                self.read_pdf(row)
                available = True
            except (OSError,LiteratureError):
                message = '全文文件校验失败，已停止下载；请重新提交求助或联系管理员。'
        return {'id':row['id'],'kind':'literature','title':row['title'],'canonical_title':row['canonical_title'],
                'doi':row['doi'],'state':row['state'],'message':message,'created_at':iso(row['created_at']),
                'deadline':iso(row['deadline']),'ready_at':iso(row['ready_at']),'download_expires_at':iso(row['download_expires_at']),
                'download_available':available,'file_expired':bool(row['file_expired'] or (row['download_expires_at'] and row['download_expires_at']<=time.time())),
                'credit_status':row['credit_status'],'candidates':json.loads(row['candidates']),
                'stage':row['stage'],'last_checked_at':iso(row['last_checked_at']),
                'next_check_at':iso(row['next_poll']) if row['state'] in ('queued','waiting','needs_attention') and row['next_poll'] else None}

    def enqueue(self, db, user_id, title, request_key):
        self.maintain(db)
        self.grant(db,user_id)
        key = normalize_title(title)
        if len(key)<6 or any(ord(c)<32 for c in title):
            raise LiteratureError(422,'请输入完整文章标题，至少6个有效字符，不能含控制字符。')
        previous = db.execute('SELECT * FROM literature_request_keys WHERE user_id=? AND request_key=?', (user_id,request_key)).fetchone()
        if previous:
            if previous['title_key'] != key:
                raise LiteratureError(409,'请求标识已用于另一篇文献。')
            return self.view(self.own(db,previous['job_id'],user_id))
        if not json.loads(db.execute("SELECT value FROM settings WHERE key='literature_enabled'").fetchone()[0]):
            raise LiteratureError(503,'文献求助暂未开放，请稍后再试。')
        matches = db.execute("SELECT * FROM literature_jobs WHERE user_id=? AND title_key=? AND (state IN ('queued','running','waiting','needs_selection','needs_attention') OR (state='succeeded' AND download_expires_at>? AND artifact_path IS NOT NULL)) ORDER BY created_at DESC", (user_id,key,time.time())).fetchall()
        existing = next((row for row in matches if row['state']!='succeeded' or self.view(row)['download_available']),None)
        if existing is not None:
            db.execute('INSERT INTO literature_request_keys VALUES (?,?,?,?)', (user_id,request_key,key,existing['id']))
            return self.view(existing)
        if self.wallet(db,user_id)['available']<1:
            raise LiteratureError(402,'可用积分不足1分，可每日签到获得积分。')
        job_id, now = uuid.uuid4().hex,time.time()
        db.execute("INSERT INTO literature_jobs(id,user_id,title,title_key,state,message,created_at,deadline,credit_status) VALUES (?,?,?,?,'queued','已排队，冻结1积分；成功取文后扣除',?,?,'frozen')", (job_id,user_id,title,key,now,now+86400))
        db.execute('INSERT INTO literature_request_keys VALUES (?,?,?,?)', (user_id,request_key,key,job_id))
        self.entry(db,user_id,'freeze',-1,1,'freeze:'+job_id,job_id)
        return self.view(self.own(db,job_id,user_id))

    def status(self, db):
        ready = db.execute('SELECT 1 FROM literature_workers WHERE last_seen>? AND accounts_ready=1', (time.time()-90,)).fetchone()
        enabled = json.loads(db.execute("SELECT value FROM settings WHERE key='literature_enabled'").fetchone()[0])
        return {'enabled':enabled,'worker_online':bool(ready),'message': '文献求助可用' if enabled and ready else ('文献服务暂时不可用，任务将排队等待' if enabled else '文献求助筹备中')}

    def claim(self, db, worker_id, accounts_ready):
        self.maintain(db)
        now = time.time()
        db.execute('INSERT INTO literature_workers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,accounts_ready=excluded.accounts_ready', (worker_id,now,int(accounts_ready)))
        if not accounts_ready or not self.status(db)['enabled'] or db.execute('SELECT 1 FROM literature_jobs WHERE lease_until>?', (now,)).fetchone():
            return {'job':None}
        row = db.execute("SELECT * FROM literature_jobs WHERE state IN ('queued','waiting','needs_attention') AND deadline>? AND next_poll<=? AND lease_hash IS NULL ORDER BY next_poll,created_at LIMIT 1", (now,now)).fetchone()
        if not row:
            return {'job':None}
        from .security import new_token,digest
        token = new_token()
        db.execute("UPDATE literature_jobs SET state='running',lease_hash=?,lease_until=?,worker_id=?,message='正在处理文献求助' WHERE id=?", (digest(token),now+180,worker_id,row['id']))
        return {'job':{'id':row['id'],'title':row['title'],'canonical_title':row['canonical_title'],'doi':row['doi'],
                       'external_request_id':row['external_request_id'],'account_ref':row['account_ref'],'deadline':row['deadline'],
                       'created_at':row['created_at'],'source_revision':row['source_revision'],
                       'selected_candidate':json.loads(row['selected_candidate']) if row['selected_candidate'] else None},
                'lease_token':token,'lease_seconds':180}

    def leased(self, db, job_id, token):
        import hmac
        from .security import digest
        row = db.execute('SELECT * FROM literature_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['state']!='running' or not row['lease_hash'] or not row['lease_until'] or row['lease_until']<=time.time() or row['deadline']<=time.time() or not hmac.compare_digest(row['lease_hash'],digest(token)):
            raise LiteratureError(409,'文献任务租约已失效、已取消或已超时。')
        return row

    @staticmethod
    def identity(row, body):
        if body.source_revision != row['source_revision'] and (body.source_revision is not None or row['source_revision'] != 0):
            raise LiteratureError(409,'文献任务来源版本已改变，请重新领取任务。')
        canonical = body.canonical_title or row['canonical_title'] or row['title']
        expected_title = row['canonical_title'] or row['title']
        if body.canonical_title and not title_matches(expected_title,body.canonical_title):
            raise LiteratureError(422,'论文标题与已绑定的请求不符。')
        doi = body.doi or row['doi']
        if row['doi'] and body.doi and row['doi'].casefold()!=body.doi.casefold():
            raise LiteratureError(422,'论文 DOI 与已选候选不符。')
        if body.external_request_id and row['external_request_id'] and body.external_request_id!=row['external_request_id']:
            raise LiteratureError(409,'禁止替换已经绑定的科研通求助编号。')
        if body.account_ref and row['account_ref'] and body.account_ref!=row['account_ref']:
            raise LiteratureError(409,'禁止替换已经绑定的科研通账号。')
        return canonical,doi

    def prepare_pdf(self, row, body):
        """解析和暂存不占数据库写锁；结算前仍须用新事务复核租约和身份。"""
        import base64,binascii,os
        canonical,doi = self.identity(row,body)
        try:
            data = base64.b64decode(body.pdf_base64 or '',validate=True)
            proof = validate_pdf(data,canonical,doi)
        except (ValueError,binascii.Error) as exc:
            raise LiteratureError(422,str(exc)) from None
        destination = self.artifact({'id':row['id'],'artifact_path':f"output/literature/{row['id']}/fulltext.pdf"})
        directory = destination.parent
        directory.mkdir(parents=True,exist_ok=True)
        os.chmod(directory,0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory,prefix='.fulltext-',suffix='.pending',delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary,0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return {'path':temporary,'proof':proof,'canonical_title':canonical,'doi':doi}
        except BaseException:
            if temporary is not None:temporary.unlink(missing_ok=True)
            raise

    def update(self, db, job_id, body, prepared=None):
        self.maintain(db)
        row = self.leased(db,job_id,body.lease_token)
        canonical,doi = self.identity(row,body)
        if body.stage == 'ready' and body.state != 'succeeded':
            raise LiteratureError(409,'全文须通过服务器核验后才可下载。')
        if body.source_reset is not None:
            reset = body.source_reset
            if (body.state != 'waiting' or body.pdf_base64 is not None or body.candidates is not None
                    or row['artifact_path'] or json.loads(row['candidates']) or row['credit_status'] != 'frozen'):
                raise LiteratureError(409,'当前任务不允许接续文献来源。')
            if (reset.source_revision != row['source_revision'] or not row['external_request_id']
                    or reset.previous_request_id != row['external_request_id']):
                raise LiteratureError(409,'文献任务来源版本或原求助编号不符。')
            now = time.time()
            detail = {'job_id':job_id,'worker_id':row['worker_id'],
                      'previous_request_id':row['external_request_id'],'source_revision':row['source_revision'],
                      'reason':reset.reason,'evidence_sha256':reset.evidence_sha256}
            # 摘要用于关联执行器保存的检查记录，不作为论文或来源身份的独立证明。
            db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,'literature_source_reset',?,?)",
                       (row['user_id'],dumps(detail),now))
            db.execute("UPDATE literature_jobs SET source_revision=source_revision+1,external_request_id=NULL,stage='renewing',state='waiting',next_poll=?,last_checked_at=?,lease_hash=NULL,lease_until=NULL WHERE id=?",
                       (now,now,job_id))
            return self.view(self.own(db,job_id,row['user_id']))
        # 元数据暂时失败时不能将原始输入标成已核验题名，否则续接会跳过身份识别。
        canonical_to_store=canonical if body.canonical_title or row['canonical_title'] or body.state=='succeeded' else None
        db.execute('UPDATE literature_jobs SET canonical_title=?,doi=?,external_request_id=COALESCE(external_request_id,?),account_ref=COALESCE(account_ref,?) WHERE id=?', (canonical_to_store,doi,body.external_request_id,body.account_ref,job_id))
        db.execute('UPDATE literature_jobs SET stage=COALESCE(?,stage),last_checked_at=? WHERE id=?',
                   (body.stage,time.time(),job_id))
        if body.state=='succeeded':
            import os
            if prepared is None or prepared['canonical_title']!=canonical or prepared['doi']!=doi:
                raise LiteratureError(409,'核验期间任务身份已改变，请重新核验。')
            temporary,proof = prepared['path'],prepared['proof']
            destination = self.artifact({'id':job_id,'artifact_path':f'output/literature/{job_id}/fulltext.pdf'})
            if temporary.parent!=destination.parent or temporary.is_symlink():
                raise LiteratureError(422,'文献暂存路径无法核验。')
            self.leased(db,job_id,body.lease_token)
            os.replace(temporary,destination)
            try:
                self.leased(db,job_id,body.lease_token)
            except LiteratureError:
                destination.unlink(missing_ok=True)
                raise
            now = time.time()
            db.execute("UPDATE literature_jobs SET artifact_path=?,artifact_sha256=?,artifact_size=?,ready_at=?,download_expires_at=?,stage='ready' WHERE id=?", (f'output/literature/{job_id}/fulltext.pdf',proof['sha256'],proof['size'],now,now+3*86400,job_id))
            self.settle(db,row,'succeeded','全文已核验，已扣1积分；请在3天内下载。')
            self.queue_reminder(db,row)
        elif body.state=='failed':
            self.settle(db,row,'failed',body.message or '文献求助失败，冻结积分已退回。')
        elif body.state=='needs_selection':
            candidates = [item.model_dump() for item in body.candidates or []]
            if not candidates or len({item['id'] for item in candidates})!=len(candidates):
                raise LiteratureError(422,'候选列表缺失或身份不唯一。')
            db.execute('UPDATE literature_jobs SET state=?,message=?,candidates=?,lease_hash=NULL,lease_until=NULL WHERE id=?', (body.state,body.message or '存在多个匹配结果，请确认需要的论文。',dumps(candidates),job_id))
        else:
            db.execute('UPDATE literature_jobs SET state=?,message=?,next_poll=?,lease_hash=NULL,lease_until=NULL WHERE id=?', (body.state,body.message or '正在等待科研通应助',time.time()+(60 if body.state=='waiting' else 300),job_id))
        return self.view(self.own(db,job_id,row['user_id']))
