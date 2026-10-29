"""管理员用户/积分管理与纯文本公告；旧账号、配置与账本保持原结构。"""
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Literal
import unicodedata
import uuid

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from highscore_downloader.numbers import parse_codes
from .credits import Credits, CreditError

BASE = '/highscore/api'
ACTIONS = {'admin_credit_adjustment', 'announcement_create', 'announcement_edit',
           'announcement_publish', 'announcement_withdraw'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS admin_credit_adjustments (
 id TEXT PRIMARY KEY REFERENCES credit_entries(id), user_id TEXT NOT NULL REFERENCES users(id),
 operator_id TEXT NOT NULL REFERENCES users(id), operator_email TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount!=0 AND amount BETWEEN -1000000 AND 1000000),
 reason TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(operator_id,request_key)
);
CREATE INDEX IF NOT EXISTS admin_adjustment_user ON admin_credit_adjustments(user_id,created_at);
CREATE TABLE IF NOT EXISTS announcements (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','published')), revision INTEGER NOT NULL CHECK(revision>0),
 created_at REAL NOT NULL, updated_at REAL NOT NULL, published_at REAL,
 created_by TEXT NOT NULL REFERENCES users(id), request_key TEXT NOT NULL, creation_fingerprint TEXT NOT NULL,
 UNIQUE(created_by,request_key)
);
CREATE INDEX IF NOT EXISTS announcement_public ON announcements(status,published_at,id);
CREATE TABLE IF NOT EXISTS admin_activity (
 id INTEGER PRIMARY KEY REFERENCES audit(id), actor_id TEXT NOT NULL REFERENCES users(id),
 target_id TEXT REFERENCES users(id), action TEXT NOT NULL,
 amount INTEGER, reason TEXT, title TEXT, created_at REAL NOT NULL
);
'''


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def text_value(value, multiline=False):
    allowed = '\r\n\t' if multiline else ''
    if not value.strip() or any(unicodedata.category(char) in ('Cc', 'Cs') and char not in allowed for char in value):
        raise ValueError('内容不能为空白或包含不支持的控制字符。')
    return value


class Adjustment(Model):
    amount: int = Field(ge=-1000000, le=1000000)
    reason: str = Field(min_length=1, max_length=200)
    request_key: str = Field(min_length=1, max_length=160)

    @field_validator('amount')
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError('调整积分不能为零。')
        return value

    @field_validator('reason', 'request_key')
    @classmethod
    def clean_text(cls, value):
        return text_value(value)


class AnnouncementCreate(Model):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    request_key: str = Field(min_length=1, max_length=160)

    @field_validator('title', 'request_key')
    @classmethod
    def single_line(cls, value):
        return text_value(value)

    @field_validator('body')
    @classmethod
    def body_text(cls, value):
        return text_value(value, multiline=True)


class AnnouncementEdit(Model):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    status: Literal['draft', 'published']
    revision: int = Field(ge=1, le=2147483647)

    @field_validator('title')
    @classmethod
    def title_text(cls, value):
        return text_value(value)

    @field_validator('body')
    @classmethod
    def body_text(cls, value):
        return text_value(value, multiline=True)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def current_admin(db, actor):
    row = db.execute('SELECT id,email FROM users WHERE id=? AND verified=1 AND is_admin=1', (actor['id'],)).fetchone()
    if row is None:
        raise HTTPException(403, '仅管理员可执行此操作。')
    return row


def record_activity(db, actor, action, *, target=None, amount=None, reason=None, title=None):
    if action not in ACTIONS:
        raise ValueError('未知管理审计动作。')
    now = time.time()
    detail = {key: value for key, value in {'target_user_id': target, 'amount': amount,
        'reason': reason, 'title': title}.items() if value is not None}
    cursor = db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)',
                        (actor['id'], action, encoded(detail), now))
    db.execute('INSERT INTO admin_activity VALUES (?,?,?,?,?,?,?,?)',
               (cursor.lastrowid, actor['id'], target, action, amount, reason, title, now))


def activity_page(db, offset, limit):
    total = db.execute('SELECT count(*) FROM admin_activity WHERE action IN (?,?,?,?,?)', tuple(sorted(ACTIONS))).fetchone()[0]
    rows = db.execute('SELECT a.id,a.action,a.amount,a.reason,a.title,a.created_at,u.email AS actor_email,'
        't.email AS target_email FROM admin_activity a JOIN users u ON u.id=a.actor_id '
        'LEFT JOIN users t ON t.id=a.target_id WHERE a.action IN (?,?,?,?,?) '
        'ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?', (*sorted(ACTIONS), limit, offset))
    items = [{**{key: row[key] for key in ('id', 'action', 'amount', 'reason', 'title', 'actor_email', 'target_email')},
              'created_at': iso(row['created_at'])} for row in rows]
    return {'items': items, 'total': total, 'next_offset': offset + len(items) if offset + len(items) < total else None}


USER_QUERY = '''SELECT u.id,u.email,u.verified,u.is_admin,u.created_at,
 (d.user_id IS NOT NULL) AS is_debug,d.fixed_feature AS debug_feature,
 COALESCE(c.available,0) AS available,COALESCE(c.frozen,0) AS frozen,
 (SELECT count(*) FROM jobs j WHERE j.user_id=u.id AND j.kind IN ('search','archive','mail_retry')) AS xrd_jobs,
 (SELECT count(*) FROM literature_jobs l WHERE l.user_id=u.id) AS literature_jobs
 FROM users u LEFT JOIN (SELECT user_id,SUM(amount) AS available,SUM(frozen_amount) AS frozen
 FROM credit_entries GROUP BY user_id) c ON c.user_id=u.id
 LEFT JOIN debug_accounts d ON d.user_id=u.id'''


def user_view(row):
    return {**{key: row[key] for key in ('id', 'email', 'available', 'frozen', 'xrd_jobs', 'literature_jobs')},
            'verified': bool(row['verified']), 'is_admin': bool(row['is_admin']), 'created_at': iso(row['created_at']),
            'is_debug': bool(row['is_debug']), 'debug_feature': row['debug_feature']}


def recent_jobs(db, user_id):
    items = []
    labels = {'search': '卡片查询', 'archive': '卡片归档', 'mail_retry': '报告邮件'}
    for row in db.execute("SELECT id,kind,state,created_at,archive_status,mail_status,payload FROM jobs "
            "WHERE user_id=? AND kind IN ('search','archive','mail_retry') ORDER BY created_at DESC,id DESC LIMIT 20", (user_id,)):
        codes = []
        try:
            payload = json.loads(row['payload'])
            text = payload.get('query', '') if row['kind'] == 'search' and payload.get('mode') == 'codes' else payload.get('codes', '')
            codes = [value.canonical_hint or value.query for value in parse_codes(text)] if text else []
        except (ValueError, TypeError, AttributeError):
            pass
        items.append({'id': row['id'], 'kind': row['kind'], 'state': row['state'], 'title': labels[row['kind']],
            'codes': codes, 'created_at': iso(row['created_at']), 'archive_status': row['archive_status'],
            'mail_status': row['mail_status'], '_sort_time': row['created_at']})
    for row in db.execute('SELECT id,state,title,canonical_title,created_at FROM literature_jobs '
            'WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 20', (user_id,)):
        items.append({'id': row['id'], 'kind': 'literature', 'state': row['state'],
            'title': row['canonical_title'] or row['title'], 'created_at': iso(row['created_at']), '_sort_time': row['created_at']})
    items.sort(key=lambda item: (item['_sort_time'], item['id']), reverse=True)
    return [{key: value for key, value in item.items() if key != '_sort_time'} for item in items[:20]]


def announcement_view(row, public=False):
    value = {key: row[key] for key in ('id', 'title', 'body', 'revision')}
    value['published_at'] = iso(row['published_at'])
    if not public:
        value.update(status=row['status'], created_at=iso(row['created_at']), updated_at=iso(row['updated_at']))
    return value


def announcement_page(db, offset, limit, public=False):
    where = " WHERE status='published'" if public else ''
    order = 'published_at DESC,id DESC' if public else 'updated_at DESC,id DESC'
    total = db.execute('SELECT count(*) FROM announcements' + where).fetchone()[0]
    rows = db.execute('SELECT * FROM announcements' + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', (limit, offset))
    items = [announcement_view(row, public) for row in rows]
    return {'items': items, 'total': total, 'next_offset': offset + len(items) if offset + len(items) < total else None}


def adjustment_view(row):
    return {**{key: row[key] for key in ('id', 'user_id', 'amount', 'reason', 'operator_email')}, 'created_at': iso(row['created_at'])}


def balance(db, user_id):
    row = db.execute('SELECT COALESCE(SUM(amount),0),COALESCE(SUM(frozen_amount),0) FROM credit_entries WHERE user_id=?', (user_id,)).fetchone()
    return {'available': row[0], 'frozen': row[1]}


def mount_admin_center(app, database, admin, admin_writer):
    @app.get(BASE + '/admin/overview')
    def overview(actor=Depends(admin)):
        with database.connection() as db:
            db.execute('BEGIN')
            current_admin(db, actor)
            users = db.execute('SELECT count(*),COALESCE(SUM(verified),0),COALESCE(SUM(is_admin),0) FROM users').fetchone()
            regular = db.execute('SELECT count(*),COALESCE(SUM(u.verified),0),COALESCE(SUM(u.is_admin),0) '
                'FROM users u WHERE NOT EXISTS (SELECT 1 FROM debug_accounts d WHERE d.user_id=u.id)').fetchone()
            xrd = db.execute("SELECT count(*),COALESCE(SUM(state='queued'),0),COALESCE(SUM(state='running'),0) FROM jobs WHERE kind IN ('search','archive','mail_retry')").fetchone()
            lit = db.execute("SELECT count(*),COALESCE(SUM(state='queued'),0),COALESCE(SUM(state='running'),0) FROM literature_jobs").fetchone()
            credits = db.execute('SELECT COALESCE(SUM(amount),0),COALESCE(SUM(frozen_amount),0) FROM credit_entries').fetchone()
            return {'users': {**dict(zip(('total', 'verified', 'admins'), users)),
                **dict(zip(('regular_total', 'regular_verified', 'regular_admins'), regular)), 'debug_total': users[0]-regular[0]},
                'jobs': {'xrd_total': xrd[0], 'literature_total': lit[0], 'queued': xrd[1] + lit[1], 'running': xrd[2] + lit[2]},
                'credits': {'available': credits[0], 'frozen': credits[1]}, 'recent_activity': activity_page(db, 0, 10)['items']}

    @app.get(BASE + '/admin/users')
    def users(q: str = Query(default='', max_length=254), offset: int = Query(default=0, ge=0, le=2147483647),
              limit: int = Query(default=20, ge=1, le=50),
              account_type: Literal['regular', 'debug', 'all'] = 'regular', actor=Depends(admin)):
        with database.connection() as db:
            db.execute('BEGIN')
            current_admin(db, actor)
            db.create_function('admin_casefold', 1, lambda value: value.casefold())
            query = q.strip().casefold()
            predicates, args = [], ()
            if query:
                predicates.append('instr(admin_casefold(u.email),?)>0')
                args = (query,)
            if account_type != 'all':
                predicates.append(('NOT ' if account_type == 'regular' else '') +
                    'EXISTS (SELECT 1 FROM debug_accounts marked WHERE marked.user_id=u.id)')
            where = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
            total = db.execute('SELECT count(*) FROM users u' + where, args).fetchone()[0]
            rows = db.execute(USER_QUERY + where + ' ORDER BY u.created_at DESC,u.id DESC LIMIT ? OFFSET ?', (*args, limit, offset))
            items = [user_view(row) for row in rows]
            return {'items': items, 'total': total, 'next_offset': offset + len(items) if offset + len(items) < total else None}

    @app.get(BASE + '/admin/users/{user_id}')
    def detail(user_id: str, actor=Depends(admin)):
        with database.connection() as db:
            db.execute('BEGIN')
            current_admin(db, actor)
            row = db.execute(USER_QUERY + ' WHERE u.id=?', (user_id,)).fetchone()
            if row is None:
                raise HTTPException(404, '用户不存在。')
            wallet = Credits.wallet(db, user_id, limit=None)
            adjustments = {row['id']: row for row in db.execute('SELECT id,reason,operator_email FROM admin_credit_adjustments WHERE user_id=?', (user_id,))}
            for item in wallet['items']:
                if item['id'] in adjustments:
                    record = adjustments[item['id']]
                    item.update(reason=record['reason'], operator_email=record['operator_email'])
            return {'user': user_view(row), 'credits': wallet, 'recent_jobs': recent_jobs(db, user_id)}

    @app.post(BASE + '/admin/users/{user_id}/credits')
    def adjust(user_id: str, body: Adjustment, actor=Depends(admin_writer)):
        with database.connection(write=True) as db:
            operator = current_admin(db, actor)
            identity = fingerprint({'user_id': user_id, 'amount': body.amount, 'reason': body.reason})
            prior = db.execute('SELECT * FROM admin_credit_adjustments WHERE operator_id=? AND request_key=?', (operator['id'], body.request_key)).fetchone()
            if prior:
                if prior['fingerprint'] != identity:
                    raise HTTPException(409, '请求标识已用于不同的积分调整，请重新核对。')
                return {'adjustment': adjustment_view(prior), 'credits': balance(db, user_id)}
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise HTTPException(404, '用户不存在。')
            identifier, now = uuid.uuid4().hex, time.time()
            try:
                Credits.adjust(db, user_id, identifier, body.amount)
            except CreditError as exc:
                raise HTTPException(409, str(exc)) from None
            db.execute('INSERT INTO admin_credit_adjustments VALUES (?,?,?,?,?,?,?,?,?)',
                (identifier, user_id, operator['id'], operator['email'], body.amount, body.reason, body.request_key, identity, now))
            record_activity(db, operator, 'admin_credit_adjustment', target=user_id, amount=body.amount, reason=body.reason, title='管理员调整积分')
            result = db.execute('SELECT * FROM admin_credit_adjustments WHERE id=?', (identifier,)).fetchone()
            return {'adjustment': adjustment_view(result), 'credits': balance(db, user_id)}

    @app.get(BASE + '/admin/activity')
    def activity(offset: int = Query(default=0, ge=0, le=2147483647),
                 limit: int = Query(default=20, ge=1, le=50), actor=Depends(admin)):
        with database.connection() as db:
            db.execute('BEGIN')
            current_admin(db, actor)
            return activity_page(db, offset, limit)

    @app.get(BASE + '/announcements')
    def public_announcements(offset: int = Query(default=0, ge=0, le=2147483647), limit: int = Query(default=20, ge=1, le=50)):
        with database.connection() as db:
            db.execute('BEGIN')
            return announcement_page(db, offset, limit, public=True)

    @app.get(BASE + '/admin/announcements')
    def announcements(offset: int = Query(default=0, ge=0, le=2147483647),
                      limit: int = Query(default=20, ge=1, le=50), actor=Depends(admin)):
        with database.connection() as db:
            db.execute('BEGIN')
            current_admin(db, actor)
            return announcement_page(db, offset, limit)

    @app.post(BASE + '/admin/announcements')
    def create_announcement(body: AnnouncementCreate, actor=Depends(admin_writer)):
        with database.connection(write=True) as db:
            operator = current_admin(db, actor)
            identity = fingerprint({'title': body.title, 'body': body.body})
            prior = db.execute('SELECT * FROM announcements WHERE created_by=? AND request_key=?', (operator['id'], body.request_key)).fetchone()
            if prior:
                if prior['creation_fingerprint'] != identity:
                    raise HTTPException(409, '请求标识已用于不同公告，请重新核对。')
                return announcement_view(prior)
            identifier, now = uuid.uuid4().hex, time.time()
            db.execute("INSERT INTO announcements VALUES (?,?,?,'draft',1,?,?,NULL,?,?,?)",
                (identifier, body.title, body.body, now, now, operator['id'], body.request_key, identity))
            record_activity(db, operator, 'announcement_create', title=body.title)
            return announcement_view(db.execute('SELECT * FROM announcements WHERE id=?', (identifier,)).fetchone())

    @app.patch(BASE + '/admin/announcements/{announcement_id}')
    def edit_announcement(announcement_id: str, body: AnnouncementEdit, actor=Depends(admin_writer)):
        with database.connection(write=True) as db:
            operator = current_admin(db, actor)
            prior = db.execute('SELECT * FROM announcements WHERE id=?', (announcement_id,)).fetchone()
            if prior is None:
                raise HTTPException(404, '公告不存在。')
            if prior['revision'] != body.revision:
                raise HTTPException(409, '公告已被修改，请重新加载后再编辑。')
            if (prior['title'], prior['body'], prior['status']) == (body.title, body.body, body.status):
                raise HTTPException(409, '公告内容未发生变化，请重新加载。')
            now = time.time()
            published_at = now if body.status == 'published' and prior['status'] != 'published' else prior['published_at']
            db.execute('UPDATE announcements SET title=?,body=?,status=?,revision=revision+1,updated_at=?,published_at=? WHERE id=? AND revision=?',
                (body.title, body.body, body.status, now, published_at, announcement_id, body.revision))
            action = ('announcement_publish' if body.status == 'published' else 'announcement_withdraw') if prior['status'] != body.status else 'announcement_edit'
            record_activity(db, operator, action, title=body.title)
            return announcement_view(db.execute('SELECT * FROM announcements WHERE id=?', (announcement_id,)).fetchone())
