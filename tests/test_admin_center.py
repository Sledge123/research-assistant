"""系统管理中心真实SQLite/API离线回归，不连接外部服务。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from highscore_web import create_app
from highscore_web.credits import Credits

BASE = '/highscore/api'
ROOT = Path(__file__).resolve().parents[1]


class AdminCenterTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tmp'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=cache, prefix='admin-center-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
            'database_admin_only': False, 'worker_token': 'synthetic-admin-center-worker-token-123456'}
        self.app = create_app(self.root, self.config)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'X-CSRF-Token': 'csrf-admin'}
        with self.app.state.database.connection(write=True) as db:
            for number, (identifier, email, verified, admin) in enumerate((
                    ('admin', 'admin@example.test', 1, 1), ('user', '材料.User@Example.test', 1, 0),
                    ('pending', 'pending@example.test', 0, 0), ('other-admin', 'other-admin@example.test', 1, 1))):
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)',
                    (identifier, email, 'secret-password-hash', verified, admin, time.time() + number))
                token = 'session-' + identifier
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(), identifier, 'csrf-' + identifier, time.time() + 3600))
            Credits.grant(db, 'user')
            db.execute("UPDATE settings SET value='true' WHERE key IN ('license_confirmed','cloud_validated','public_enabled','literature_enabled')")
            db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES ('admin','legacy-secret','secret-csrf-token',?)", (time.time(),))
        self.client.cookies.set('highscore_session', 'session-admin')

    def as_user(self, identifier):
        client = TestClient(self.app)
        self.addCleanup(client.close)
        client.cookies.set('highscore_session', 'session-' + identifier)
        return client, {'X-CSRF-Token': 'csrf-' + identifier}

    def adjust(self, amount=5, reason='人工核对补分', key='adjust', user='user', **extra):
        return self.client.post(BASE + '/admin/users/' + user + '/credits', headers=self.headers,
            json={'amount': amount, 'reason': reason, 'request_key': key, **extra})

    def draft(self, key='notice', title='科研服务公告', body='第一行\n第二行'):
        return self.client.post(BASE + '/admin/announcements', headers=self.headers,
            json={'title': title, 'body': body, 'request_key': key})

    def test_admin_authentication_csrf_origin_and_live_role_are_enforced(self):
        anonymous = TestClient(self.app)
        self.addCleanup(anonymous.close)
        ordinary, h = self.as_user('user')
        for path in ('/admin/overview', '/admin/users', '/admin/users/user', '/admin/activity', '/admin/announcements'):
            self.assertEqual(anonymous.get(BASE + path).status_code, 401)
            self.assertEqual(ordinary.get(BASE + path).status_code, 403)
        body = {'amount': 1, 'reason': '越权请求', 'request_key': 'no'}
        self.assertEqual(ordinary.post(BASE + '/admin/users/user/credits', headers=h, json=body).status_code, 403)
        self.assertEqual(self.client.post(BASE + '/admin/users/user/credits', json=body).status_code, 403)
        self.assertEqual(self.client.post(BASE + '/admin/users/user/credits', headers={**self.headers, 'Origin': 'https://other.invalid'}, json=body).status_code, 403)
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET is_admin=0 WHERE id='admin'")
        self.assertEqual(self.adjust().status_code, 403)

    def test_overview_user_search_and_details_are_safe_and_read_only(self):
        with self.app.state.database.connection(write=True) as db:
            now = time.time()
            for identifier, kind, state in (('xrd', 'archive', 'queued'), ('mail', 'verification_mail', 'queued')):
                db.execute('INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (identifier, 'user', kind, state, json.dumps({'codes': '00-046-1045', 'secret': 'payload-secret'}), 'f', identifier, now, now))
            db.execute('INSERT INTO literature_jobs(id,user_id,title,title_key,state,message,created_at,deadline,credit_status,account_ref) VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('lit', 'user', '安全的中文论文标题', 'title-key', 'running', 'internal-secret', now, now + 3600, 'frozen', 'secret-account'))
            before = db.execute('SELECT count(*) FROM credit_entries').fetchone()[0]
        overview = self.client.get(BASE + '/admin/overview')
        self.assertEqual(overview.status_code, 200, overview.text)
        value = overview.json()
        self.assertEqual(value['users'], {'total': 4, 'verified': 3, 'admins': 2,
            'regular_total': 4, 'regular_verified': 3, 'regular_admins': 2, 'debug_total': 0})
        self.assertEqual(value['jobs'], {'xrd_total': 1, 'literature_total': 1, 'queued': 1, 'running': 1})
        self.assertEqual(value['credits'], {'available': 10, 'frozen': 0})
        page = self.client.get(BASE + '/admin/users', params={'q': '材料.user@EXAMPLE', 'limit': 1}).json()
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['id'], 'user')
        self.assertEqual(page['items'][0]['xrd_jobs'], 1)
        self.assertEqual(page['items'][0]['literature_jobs'], 1)
        self.assertEqual(self.client.get(BASE + '/admin/users', params={'q': '%'}).json()['total'], 0)
        detail = self.client.get(BASE + '/admin/users/user').json()
        self.assertEqual({x['id'] for x in detail['recent_jobs']}, {'xrd', 'lit'})
        serialized = json.dumps([value, page, detail], ensure_ascii=False)
        for secret in ('password_hash', 'secret-password-hash', 'csrf', 'payload-secret', 'secret-account', 'internal-secret', 'lease', 'artifact_path'):
            self.assertNotIn(secret, serialized)
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM credit_entries').fetchone()[0], before)
        self.assertEqual(self.client.get(BASE + '/admin/users/missing').status_code, 404)
        self.assertEqual(self.client.get(BASE + '/admin/activity').json()['items'], [])

    def test_pagination_boundaries_and_stable_user_pages(self):
        first = self.client.get(BASE + '/admin/users', params={'limit': 2}).json()
        second = self.client.get(BASE + '/admin/users', params={'limit': 2, 'offset': first['next_offset']}).json()
        self.assertEqual(first['total'], 4)
        self.assertEqual(len({x['id'] for x in first['items'] + second['items']}), 4)
        self.assertIsNone(second['next_offset'])
        for path in ('/admin/users', '/admin/activity', '/admin/announcements', '/announcements'):
            for params in ({'limit': 0}, {'limit': 51}, {'offset': -1}, {'offset': 10**60}, {'limit': 'bad'}):
                self.assertEqual(self.client.get(BASE + path, params=params).status_code, 422)

    def test_credit_adjustment_is_strict_audited_and_idempotent(self):
        for amount in (True, 1.0, '2', 0, 1000001, -1000001):
            self.assertEqual(self.adjust(amount=amount).status_code, 422)
        for reason in ('', '   ', '\x00', 'a' * 201):
            self.assertEqual(self.adjust(reason=reason).status_code, 422)
        self.assertEqual(self.adjust(operator_id='other-admin').status_code, 422)
        first = self.adjust()
        self.assertEqual(first.status_code, 200, first.text)
        repeated = self.adjust()
        self.assertEqual(first.json(), repeated.json())
        self.assertEqual(first.json()['credits'], {'available': 15, 'frozen': 0})
        self.assertEqual(first.json()['adjustment']['operator_email'], 'admin@example.test')
        for change in ({'amount': 6}, {'reason': '不同原因'}, {'user': 'pending'}):
            self.assertEqual(self.adjust(**change).status_code, 409)
        detail = self.client.get(BASE + '/admin/users/user').json()
        item = next(x for x in detail['credits']['items'] if x['kind'] == 'admin_adjustment')
        self.assertEqual(item['reason'], '人工核对补分')
        self.assertEqual(item['operator_email'], 'admin@example.test')
        self.assertEqual(item['label'], '管理员调整积分')
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_credit_adjustments').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_entries WHERE kind='admin_adjustment'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='admin_credit_adjustment'").fetchone()[0], 1)

    def test_concurrent_same_key_is_one_adjustment_and_different_admin_key_is_independent(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _: self.adjust(), range(5)))
        self.assertTrue(all(x.status_code == 200 for x in replies))
        self.assertEqual(len({x.json()['adjustment']['id'] for x in replies}), 1)
        other, headers = self.as_user('other-admin')
        result = other.post(BASE + '/admin/users/user/credits', headers=headers,
            json={'amount': 1, 'reason': '另一管理员', 'request_key': 'adjust'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['credits']['available'], 16)

    def test_debit_cannot_consume_frozen_points_or_change_existing_holds(self):
        client, headers = self.as_user('user')
        self.assertEqual(client.post(BASE + '/jobs/archive', headers=headers,
            json={'codes': '00-046-1045', 'request_key': 'freeze-xrd'}).status_code, 200)
        self.assertEqual(client.post(BASE + '/literature/jobs', headers=headers,
            json={'title': '冻结测试的中文论文标题', 'request_key': 'freeze-lit'}).status_code, 200)
        self.assertEqual(self.adjust(-8).json()['credits'], {'available': 0, 'frozen': 2})
        self.assertEqual(self.adjust(-1, key='overdraw').status_code, 409)
        detail = self.client.get(BASE + '/admin/users/user').json()
        self.assertEqual((detail['credits']['available'], detail['credits']['frozen']), (0, 2))
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT reserved,charged FROM xrd_credit_holds').fetchone()[:], (1, None))

    def test_adjustment_races_xrd_and_literature_without_overspending(self):
        self.assertEqual(self.adjust(-9, key='initial').status_code, 200)
        client, headers = self.as_user('user')
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.adjust, -1, key='last'),
                pool.submit(client.post, BASE + '/jobs/archive', headers=headers,
                    json={'codes': '00-046-1045', 'request_key': 'last-xrd'}),
                pool.submit(client.post, BASE + '/literature/jobs', headers=headers,
                    json={'title': '最后一分并发文献冻结测试', 'request_key': 'last-lit'})]
            codes = [x.result().status_code for x in futures]
        self.assertEqual(codes.count(200), 1, codes)
        self.assertTrue(all(x in (200, 402, 409) for x in codes), codes)
        detail = self.client.get(BASE + '/admin/users/user').json()['credits']
        self.assertEqual(detail['available'], 0)
        self.assertIn(detail['frozen'], (0, 1))

    def test_unknown_users_and_failed_adjustments_leave_no_partial_records(self):
        self.assertEqual(self.adjust(user='absent').status_code, 404)
        self.assertEqual(self.adjust(-11).status_code, 409)
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_credit_adjustments').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='admin_credit_adjustment'").fetchone()[0], 0)
        self.assertEqual(self.adjust(user='pending').json()['credits']['available'], 5)

    def test_audit_failure_rolls_back_ledger_and_adjustment_in_the_same_transaction(self):
        with patch('highscore_web.admin_center.record_activity', side_effect=RuntimeError('synthetic-audit-failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic-audit-failure'):
                self.adjust()
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_credit_adjustments').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_entries WHERE kind='admin_adjustment'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM admin_activity').fetchone()[0], 0)
        self.assertEqual(self.adjust().json()['credits']['available'], 15)

    def test_admin_detail_contains_complete_ledger_while_public_wallet_keeps_its_limit(self):
        with self.app.state.database.connection(write=True) as db:
            for index in range(201):
                Credits.entry(db, 'user', 'checkin', 1, 0, 'synthetic-history-' + str(index))
        detail = self.client.get(BASE + '/admin/users/user').json()
        self.assertEqual(len(detail['credits']['items']), 202)
        client, _ = self.as_user('user')
        self.assertEqual(len(client.get(BASE + '/credits').json()['items']), 200)

    def test_activity_is_paginated_structured_and_contains_only_new_whitelisted_actions(self):
        self.assertEqual(self.adjust().status_code, 200)
        notice = self.draft().json()
        response = self.client.patch(BASE + '/admin/announcements/' + notice['id'], headers=self.headers,
            json={'title': '修改后的公告标题', 'body': notice['body'], 'status': 'draft', 'revision': notice['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        items, offset = [], 0
        while offset is not None:
            page = self.client.get(BASE + '/admin/activity', params={'offset': offset, 'limit': 1}).json()
            self.assertEqual(page['total'], 3)
            items.extend(page['items'])
            offset = page['next_offset']
        self.assertEqual(len({x['id'] for x in items}), 3)
        self.assertEqual({x['action'] for x in items}, {'admin_credit_adjustment', 'announcement_create', 'announcement_edit'})
        adjustment = next(x for x in items if x['action'] == 'admin_credit_adjustment')
        self.assertEqual((adjustment['actor_email'], adjustment['target_email'], adjustment['amount']),
                         ('admin@example.test', '材料.User@Example.test', 5))
        self.assertNotIn('detail', json.dumps(items))
        self.assertNotIn('secret-csrf-token', json.dumps(items))

    def test_announcements_are_drafts_until_published_and_plain_text_is_preserved(self):
        raw = '<script>alert("xss")</script>\n正文 & 原文'
        created = self.draft(body=raw)
        self.assertEqual(created.status_code, 200, created.text)
        value = created.json()
        self.assertEqual(value['status'], 'draft')
        self.assertEqual(value['body'], raw)
        self.assertEqual(self.draft(body=raw).json()['id'], value['id'])
        self.assertEqual(self.draft(body='different').status_code, 409)
        self.assertEqual(self.client.get(BASE + '/announcements').json()['total'], 0)
        endpoint = BASE + '/admin/announcements/' + value['id']
        patch = {'title': value['title'], 'body': raw, 'status': 'published', 'revision': value['revision']}
        published = self.client.patch(endpoint, headers=self.headers, json=patch)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(self.client.patch(endpoint, headers=self.headers, json=patch).status_code, 409)
        public = TestClient(self.app)
        self.addCleanup(public.close)
        listing = public.get(BASE + '/announcements').json()
        self.assertEqual(listing['total'], 1)
        self.assertEqual(set(listing['items'][0]), {'id', 'title', 'body', 'published_at', 'revision'})
        self.assertEqual(listing['items'][0]['body'], raw)
        patch.update(status='draft', revision=published.json()['revision'])
        withdrawn = self.client.patch(endpoint, headers=self.headers, json=patch)
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        self.assertEqual(public.get(BASE + '/announcements').json()['total'], 0)
        actions = {x['action'] for x in self.client.get(BASE + '/admin/activity').json()['items']}
        self.assertTrue({'announcement_create', 'announcement_publish', 'announcement_withdraw'} <= actions)

    def test_announcement_validation_permissions_and_concurrent_revision(self):
        for title, body in ((' ', '正文'), ('标题', '\x00'), ('标题\n坏', '正文'), ('t' * 121, '正文'), ('标题', 'b' * 10001)):
            self.assertEqual(self.draft(title=title, body=body).status_code, 422)
        ordinary, headers = self.as_user('user')
        self.assertEqual(ordinary.post(BASE + '/admin/announcements', headers=headers,
            json={'title': '未授权公告', 'body': '正文', 'request_key': 'forged'}).status_code, 403)
        notice = self.draft().json()
        endpoint = BASE + '/admin/announcements/' + notice['id']
        for revision in (True, 1.0, '1', 0):
            self.assertEqual(self.client.patch(endpoint, headers=self.headers,
                json={'title': '新标题', 'body': '正文', 'status': 'draft', 'revision': revision}).status_code, 422)
        def edit(title):
            return self.client.patch(endpoint, headers=self.headers,
                json={'title': title, 'body': '正文', 'status': 'draft', 'revision': notice['revision']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(edit, ['并发标题甲', '并发标题乙']))
        self.assertEqual(sorted(x.status_code for x in replies), [200, 409])

    def test_full_length_chinese_announcement_and_restart_preserve_records_and_users_schema(self):
        body = '中文' * 5000
        response = self.draft(body=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.adjust().status_code, 200)
        restarted = create_app(self.root, self.config)
        client = TestClient(restarted)
        self.addCleanup(client.close)
        client.cookies.set('highscore_session', 'session-admin')
        self.assertEqual(client.get(BASE + '/admin/announcements').json()['items'][0]['body'], body)
        self.assertEqual(client.get(BASE + '/admin/users/user').json()['credits']['available'], 15)
        with restarted.state.database.connection() as db:
            self.assertEqual([x['name'] for x in db.execute('PRAGMA table_info(users)')],
                ['id', 'email', 'password_hash', 'verified', 'is_admin', 'created_at'])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_entries WHERE user_id='user' AND kind='registration'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
