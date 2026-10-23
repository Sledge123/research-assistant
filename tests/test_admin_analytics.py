"""访问统计的真实HTTP、独立持久存储及原业务保护回归。"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from highscore_web.app import create_app

BASE = '/highscore/api'
ORIGIN = 'https://site.example.test'


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        cache = Path('.cache/tmp'); cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = create_app(self.root, {'public_url': ORIGIN + '/highscore/', 'secure_cookie': False})
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'Origin': ORIGIN, 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36'}
        with self.app.state.database.connection(write=True) as db:
            for identifier, verified, admin in [('admin', 1, 1), ('user', 1, 0), ('pending', 0, 1)]:
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',
                           (identifier, identifier + '@example.invalid', 'unused', verified, admin, time.time()))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (hashlib.sha256(('test-' + identifier).encode()).hexdigest(), identifier, 'csrf', time.time() + 3600))
        self.client.cookies.set('highscore_session', 'test-admin')

    def post(self, page='literature', event_id=None, **kwargs):
        headers = {**self.headers, **kwargs.pop('headers', {})}
        return self.client.post(BASE + '/analytics/pageview', headers=headers,
            json={'page': page, 'event_id': event_id or uuid.uuid4().hex, **kwargs})

    def stats(self, period='today'):
        result = self.client.get(BASE + '/admin/analytics', params={'period': period})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_metrics_idempotence_independent_visitors_and_safe_dimensions(self):
        event = uuid.uuid4().hex
        self.assertEqual(self.post(event_id=event, referrer='www.example.org').status_code, 204)
        self.assertEqual(self.post(event_id=event, referrer='www.example.org').status_code, 204)
        self.assertEqual(self.post('search').status_code, 204)
        other = TestClient(self.app); self.addCleanup(other.close)
        self.assertEqual(other.post(BASE + '/analytics/pageview', headers=self.headers,
            json={'page': 'library', 'event_id': uuid.uuid4().hex}).status_code, 204)
        value = self.stats()
        self.assertEqual(value['summary'], {'pv': 3, 'uv': 2, 'ip': 1, 'new_visitors': 2})
        self.assertEqual(sum(x['pv'] for x in value['series']), 3)
        self.assertEqual(value['browsers'][0]['name'], 'Chrome')
        self.assertEqual(value['systems'][0]['name'], 'Windows')
        self.assertEqual(value['devices'][0]['name'], '桌面')
        self.assertIn('www.example.org', [x['name'] for x in value['referrers']])
        self.assertEqual(value['timezone'], 'Asia/Shanghai')
        self.assertEqual(len(value['series']), 24)
        self.assertTrue(value['first_collected_at'])
        self.assertEqual(self.stats('yesterday')['summary']['pv'], 0)

    def test_period_unique_counts_are_not_daily_sums_and_boundary_is_shanghai(self):
        from datetime import datetime, timezone
        yesterday = datetime(2026, 10, 20, 15, 59, tzinfo=timezone.utc).timestamp()
        today = yesterday + 120
        with patch('highscore_web.analytics.time.time', return_value=yesterday):
            self.assertEqual(self.post().status_code, 204)
        with patch('highscore_web.analytics.time.time', return_value=today):
            self.assertEqual(self.post('search').status_code, 204)
            value = self.stats('7d')
            self.assertEqual(value['summary'], {'pv': 2, 'uv': 1, 'ip': 1, 'new_visitors': 1})
            self.assertEqual(sum(x['uv'] for x in value['series']), 2)
            self.assertEqual(self.stats()['summary']['new_visitors'], 0)
            self.assertEqual(self.stats('yesterday')['summary']['pv'], 1)
            self.assertEqual(value['end_date'], '2026-10-21')

    def test_admin_only_live_verification_and_invalid_period(self):
        for identifier, expected in [('', 401), ('user', 403), ('pending', 401)]:
            self.client.cookies.clear()
            if identifier: self.client.cookies.set('highscore_session', 'test-' + identifier)
            self.assertEqual(self.client.get(BASE + '/admin/analytics').status_code, expected)
        self.client.cookies.set('highscore_session', 'test-admin')
        self.assertEqual(self.client.get(BASE + '/admin/analytics?period=all').status_code, 422)
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET is_admin=0 WHERE id='admin'")
        self.assertEqual(self.client.get(BASE + '/admin/analytics').status_code, 403)

    def test_origin_body_validation_and_privacy_opt_out(self):
        for headers in [{'Origin': 'https://other.invalid'}, {'Origin': ''}, {'Sec-Fetch-Site': 'cross-site'}]:
            self.assertEqual(self.post(headers=headers).status_code, 403)
        for value in [dict(page='?verify=secret'), dict(referrer='https://example.org/?email=secret'), dict(url='secret')]:
            body = {'page': 'search', 'event_id': uuid.uuid4().hex, **value}
            self.assertEqual(self.client.post(BASE + '/analytics/pageview', headers=self.headers, json=body).status_code, 422)
        for headers in [{'DNT': '1'}, {'Sec-GPC': '1'}, {'User-Agent': 'Googlebot/2.1'}]:
            self.assertEqual(self.post(headers=headers).status_code, 204)
        self.assertEqual(self.stats()['summary']['pv'], 0)
        result = self.client.post(BASE + '/analytics/pageview', headers=self.headers, content=b'x' * 1025)
        self.assertEqual(result.status_code, 413)

    def test_storage_hides_identifiers_and_does_not_change_business_tables(self):
        def snapshot():
            with self.app.state.database.connection() as db:
                return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')]
                    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        before = snapshot()
        self.assertEqual(self.post(headers={'X-Forwarded-For': '192.0.2.123'}).status_code, 204)
        value = self.stats()
        self.assertEqual(snapshot(), before)
        storage = self.root / 'output/web/analytics'
        with closing(sqlite3.connect(storage / 'visits.sqlite3')) as db:
            content = '\n'.join(db.iterdump())
        for secret in ['192.0.2.123', 'test-admin', 'example.invalid', 'Mozilla', 'testclient']:
            self.assertNotIn(secret, content)
            self.assertNotIn(secret, json.dumps(value))
        self.assertIn('HttpOnly', self.post().headers.get('set-cookie', ''))
        self.assertTrue((storage / 'identity.key').is_file())
        if os.name != 'nt':
            self.assertEqual(storage.stat().st_mode & 0o777, 0o700)
            for name in ['identity.key', 'visits.sqlite3']:
                self.assertEqual((storage / name).stat().st_mode & 0o777, 0o600)

    def test_collection_failure_does_not_break_business_or_fake_zero_stats(self):
        self.assertEqual(self.post().status_code, 204)
        with patch('highscore_web.analytics.AnalyticsStore.connection', side_effect=sqlite3.OperationalError('private-path-secret')):
            self.assertEqual(self.post().status_code, 204)
            result = self.client.get(BASE + '/admin/analytics')
            self.assertEqual(result.status_code, 503)
            self.assertNotIn('private-path-secret', result.text)
            self.assertEqual(self.client.get(BASE + '/health').status_code, 200)

    def test_retention_and_event_rate_are_isolated(self):
        self.assertEqual(self.post().status_code, 204)
        storage = self.root / 'output/web/analytics/visits.sqlite3'
        with closing(sqlite3.connect(storage)) as db, db:
            db.execute('UPDATE events SET created_at=?', (time.time() - 91 * 86400,))
            db.execute("DELETE FROM metadata WHERE key='pruned_day'")
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(self.stats()['summary']['pv'], 1)
        with closing(sqlite3.connect(storage)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        with closing(sqlite3.connect(storage)) as db, db:
            for _ in range(118):
                db.execute('INSERT INTO events SELECT ?,visitor,address,page,browser,system,device,referrer,created_at FROM events LIMIT 1',
                           (uuid.uuid4().hex,))
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(self.client.get(BASE + '/health').status_code, 200)

    def test_actual_client_ips_are_counted_and_visitor_cookie_survives_restart(self):
        first = TestClient(self.app, client=('192.0.2.1', 1234)); self.addCleanup(first.close)
        body = {'page': 'search', 'event_id': uuid.uuid4().hex}
        self.assertEqual(first.post(BASE + '/analytics/pageview', headers=self.headers, json=body).status_code, 204)
        self.assertEqual(first.post(BASE + '/analytics/pageview', headers=self.headers, json=body).status_code, 204)
        second = TestClient(self.app, client=('192.0.2.2', 1234)); self.addCleanup(second.close)
        self.assertEqual(second.post(BASE + '/analytics/pageview', headers=self.headers,
            json={**body, 'event_id': uuid.uuid4().hex}).status_code, 204)
        self.assertEqual(self.stats()['summary'], {'pv': 2, 'uv': 2, 'ip': 2, 'new_visitors': 2})
        restarted = create_app(self.root, {'public_url': ORIGIN + '/highscore/', 'secure_cookie': False})
        again = TestClient(restarted, client=('192.0.2.1', 1234)); self.addCleanup(again.close)
        again.cookies.update(first.cookies)
        self.assertEqual(again.post(BASE + '/analytics/pageview', headers=self.headers,
            json={**body, 'event_id': uuid.uuid4().hex}).status_code, 204)
        self.assertEqual(self.stats()['summary'], {'pv': 3, 'uv': 2, 'ip': 2, 'new_visitors': 2})

    def test_failed_analytics_initialization_keeps_site_available(self):
        root = self.root / 'blocked'; (root / 'output/web').mkdir(parents=True)
        (root / 'output/web/analytics').write_text('不可用的统计存储', encoding='utf-8')
        app = create_app(root, {'public_url': ORIGIN + '/highscore/', 'secure_cookie': False})
        client = TestClient(app); self.addCleanup(client.close)
        self.assertEqual(client.get(BASE + '/health').status_code, 200)
        self.assertEqual(client.post(BASE + '/analytics/pageview', headers=self.headers,
            json={'page': 'literature', 'event_id': uuid.uuid4().hex}).status_code, 204)


if __name__ == '__main__': unittest.main()
