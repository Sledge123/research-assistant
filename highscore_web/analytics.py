"""第一方访问统计；独立存储，不关联账号或写入业务账本。"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

BASE = '/highscore/api'
COOKIE = 'research_visitor'
RETENTION_DAYS = 90
SHANGHAI = timezone(timedelta(hours=8))
PAGES = {'literature': '文献求助', 'search': 'XRD 晶相检索', 'library': '科研资料库',
         'abtem': '电镜模拟', 'pymatgen': '材料工具', 'tasks': '任务中心', 'credits': '积分', 'account': '账号', 'admin': '系统管理中心'}
Page = Literal['literature', 'search', 'library', 'abtem', 'pymatgen', 'tasks', 'credits', 'account', 'admin']
Period = Literal['today', 'yesterday', '7d', '30d']
SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visitors (id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, visitor TEXT NOT NULL, address TEXT NOT NULL,
 page TEXT NOT NULL, browser TEXT NOT NULL, system TEXT NOT NULL, device TEXT NOT NULL,
 referrer TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS analytics_time ON events(created_at);
CREATE INDEX IF NOT EXISTS analytics_ip_time ON events(address,created_at);
CREATE INDEX IF NOT EXISTS analytics_visitor ON events(visitor);
'''


class Pageview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    event_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    page: Page
    referrer: str = Field(default='', max_length=253,
        pattern=r'^(?:|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$')


def environment(agent):
    agent = agent[:1024].lower()
    browser = next((label for marker, label in [('micromessenger', '微信'), ('edg', 'Edge'),
        ('opr/', 'Opera'), ('firefox/', 'Firefox'), ('chrome/', 'Chrome'), ('safari/', 'Safari')]
        if marker in agent), '其他')
    system = next((label for marker, label in [('android', 'Android'), ('iphone', 'iOS'),
        ('ipad', 'iPadOS'), ('windows', 'Windows'), ('macintosh', 'macOS'), ('linux', 'Linux')]
        if marker in agent), '其他')
    device = '平板' if 'ipad' in agent or ('android' in agent and 'mobile' not in agent) else (
        '手机' if 'mobile' in agent or 'iphone' in agent else '桌面')
    return browser, system, device


class AnalyticsStore:
    def __init__(self, root):
        self.directory = Path(root) / 'output/web/analytics'
        self.path = self.directory / 'visits.sqlite3'
        self.key = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            key_path = self.directory / 'identity.key'
            if not key_path.exists():
                if self.path.exists():
                    raise OSError('统计密钥缺失')
                try:
                    with key_path.open('xb') as handle:
                        os.chmod(key_path, 0o600)
                        handle.write(secrets.token_bytes(32))
                except FileExistsError:
                    pass
            self.key = key_path.read_bytes()
            if len(self.key) != 32:
                raise OSError('统计密钥无效')
            key_path.chmod(0o600)
            with self.connection() as db:
                db.executescript(SCHEMA)
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('enabled_at',?)", (str(time.time()),))
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error):
            self.key = None

    @contextmanager
    def connection(self, write=False):
        if self.key is None:
            raise sqlite3.OperationalError('统计不可用')
        # 独立文件的短等待；不阻塞业务SQLite或执行器。
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            else: db.execute('BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def digest(self, kind, value):
        if self.key is None:
            raise sqlite3.OperationalError('统计不可用')
        return hmac.new(self.key, (kind + ':' + value).encode(), hashlib.sha256).hexdigest()

    def record(self, body, visitor, address, agent):
        now = time.time()
        identity, address = self.digest('visitor', visitor), self.digest('ip', address)
        browser, system, device = environment(agent)
        with self.connection(write=True) as db:
            if db.execute('SELECT 1 FROM events WHERE id=?', (body.event_id,)).fetchone():
                return
            day = datetime.fromtimestamp(now, SHANGHAI).date().isoformat()
            pruned = db.execute("SELECT value FROM metadata WHERE key='pruned_day'").fetchone()
            if not pruned or pruned[0] != day:
                db.execute('DELETE FROM events WHERE created_at<?', (now - RETENTION_DAYS * 86400,))
                db.execute('DELETE FROM visitors WHERE last_seen<?', (now - RETENTION_DAYS * 86400,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('pruned_day',?)", (day,))
            if db.execute('SELECT count(*) FROM events WHERE address=? AND created_at>?', (address, now - 60)).fetchone()[0] >= 120:
                raise HTTPException(429, '访问记录过于频繁。')
            db.execute('INSERT INTO visitors VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',
                       (identity, now, now))
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                (body.event_id, identity, address, body.page, browser, system, device, body.referrer, now))

    def report(self, period):
        today = datetime.fromtimestamp(time.time(), SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        end = today if period == 'yesterday' else today + timedelta(days=1)
        days = 7 if period == '7d' else 30 if period == '30d' else 1
        start = end - timedelta(days=days)
        lower, upper = start.timestamp(), end.timestamp()
        metrics = 'count(*) AS pv,count(DISTINCT e.visitor) AS uv,count(DISTINCT e.address) AS ip'
        with self.connection() as db:
            summary = dict(db.execute('SELECT ' + metrics + ',count(DISTINCT CASE WHEN v.first_seen>=? AND v.first_seen<? '
                'THEN e.visitor END) AS new_visitors FROM events e JOIN visitors v ON v.id=e.visitor '
                'WHERE e.created_at>=? AND e.created_at<?', (lower, upper, lower, upper)).fetchone())
            hourly = days == 1
            stamp = '%Y-%m-%d %H:00' if hourly else '%Y-%m-%d'
            # 分桶统一使用北京时间，服务器时区不能改变业务日期。
            bucket = "strftime('" + stamp + "',e.created_at,'unixepoch','+8 hours')"
            first = "strftime('" + stamp + "',v.first_seen,'unixepoch','+8 hours')"
            rows = db.execute('SELECT ' + bucket + ' AS label,' + metrics + ',count(DISTINCT CASE WHEN ' + bucket + '=' + first +
                ' THEN e.visitor END) AS new_visitors FROM events e JOIN visitors v ON v.id=e.visitor '
                'WHERE e.created_at>=? AND e.created_at<? GROUP BY label', (lower, upper)).fetchall()
            points = {row['label']: dict(row) for row in rows}
            series = []
            for index in range(24 if hourly else days):
                moment = start + (timedelta(hours=index) if hourly else timedelta(days=index))
                label = moment.strftime('%Y-%m-%d %H:00' if hourly else '%Y-%m-%d')
                series.append(points.get(label, {'label': label, 'pv': 0, 'uv': 0, 'ip': 0, 'new_visitors': 0}))
            groups = {}
            # 标识固定，不允许客户端指定SQL列或自由页面名称。
            for key in ('page', 'browser', 'system', 'device', 'referrer'):
                rows = db.execute('SELECT e.' + key + ' AS name,' + metrics + ' FROM events e '
                    'WHERE e.created_at>=? AND e.created_at<? GROUP BY e.' + key + ' ORDER BY pv DESC,name LIMIT 20',
                    (lower, upper)).fetchall()
                groups[key] = [dict(row) for row in rows]
            for item in groups['page']:
                item['label'] = PAGES.get(item['name'], '其他')
            for item in groups['referrer']:
                if not item['name']: item['name'] = '直接或站内访问'
            beginning = db.execute('SELECT min(created_at) FROM events').fetchone()[0]
        return {'period': period, 'timezone': 'Asia/Shanghai', 'start_date': start.date().isoformat(),
            'end_date': (end - timedelta(seconds=1)).date().isoformat(), 'summary': summary, 'series': series,
            'pages': groups['page'], 'browsers': groups['browser'], 'systems': groups['system'],
            'devices': groups['device'], 'referrers': groups['referrer'], 'retention_days': RETENTION_DAYS,
            'first_collected_at': datetime.fromtimestamp(beginning, timezone.utc).isoformat() if beginning else None}


def mount_analytics(app, database, root, admin, origin, secure_cookie):
    store = AnalyticsStore(root)
    app.state.analytics = store

    @app.post(BASE + '/analytics/pageview', status_code=204)
    def collect(body: Pageview, request: Request):
        if request.headers.get('origin') != origin or request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '禁止跨站请求。')
        if request.headers.get('content-type', '').split(';')[0].lower() != 'application/json':
            raise HTTPException(415, '请求格式有误。')
        agent = request.headers.get('user-agent', '')
        if (request.headers.get('dnt') == '1' or request.headers.get('sec-gpc') == '1'
                or re.search(r'bot|spider|crawler|headless|curl/|python-requests', agent, re.I)):
            return Response(status_code=204)
        visitor = request.cookies.get(COOKIE, '')
        if not re.fullmatch('[0-9a-f]{64}', visitor): visitor = secrets.token_hex(32)
        address = request.client.host if request.client else ''
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = 'unknown'
        response = Response(status_code=204)
        try:
            store.record(body, visitor, address, agent)
            response.set_cookie(COOKIE, visitor, max_age=RETENTION_DAYS * 86400,
                secure=secure_cookie, httponly=True, samesite='lax', path='/')
        except (OSError, sqlite3.Error):
            # 统计故障只影响统计；详情留在独立存储状态，不回显路径或标识。
            pass
        return response

    @app.get(BASE + '/admin/analytics')
    def report(period: Period = '7d', actor=Depends(admin)):
        from .admin_center import current_admin
        with database.connection() as db:
            current_admin(db, actor)
        try:
            return store.report(period)
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '访问统计暂时不可用，请稍后刷新。') from None
