"""私有数据库运行限制使用真实 API、synthetic 用户和任务，不联网发信。"""
import unittest

from fastapi.testclient import TestClient
from highscore_web import create_app
import test_web_api

BASE = '/highscore/api'


class PrivateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.api = test_web_api.WebApiTests()
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)

    def private_mode(self):
        cookies = self.api.client.cookies
        app = create_app(self.api.root, {
            'public_url': 'http://testserver/highscore/',
            'worker_token': 'synthetic-test-worker-secret-123456',
            'secure_cookie': False, 'database_admin_only': True,
        })
        client = TestClient(app)
        client.cookies.update(cookies)
        self.addCleanup(client.close)
        self.api.client = client

    def archive(self, user, key):
        return self.api.client.post(BASE + '/jobs/archive', headers={'X-CSRF-Token': user['csrf_token']},
                                    json={'codes': '09-999-0001', 'send_mail': False, 'request_key': key})

    def test_verified_users_keep_registration_but_private_database_submission_is_explicitly_rejected(self):
        self.private_mode()
        user = self.api.register()
        self.assertTrue(user['verified'])
        response = self.archive(user, 'private-nonadmin')
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn('管理员', response.text)
        self.assertEqual(self.api.search(user).status_code, 503)
        self.assertTrue(self.api.client.get(BASE + '/health').json()['database_admin_only'])

    def test_admin_task_is_claimed_by_database_role_and_not_verification_role(self):
        user = self.api.register()
        self.api.enable(user)
        self.private_mode()
        response = self.archive(user, 'private-admin')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.api.claim(['verification_mail']).json()['job'])
        self.assertEqual(self.api.claim(['search', 'archive', 'mail_retry']).json()['job']['id'], response.json()['id'])

    def test_private_mode_does_not_claim_previously_queued_nonadmin_task(self):
        first = self.api.register()
        self.api.enable(first)
        old = self.archive(first, 'before-private').json()
        with self.api.db() as db:
            db.execute('UPDATE users SET is_admin=0 WHERE id=?', (first['id'],))
        admin = self.api.register('admin@example.com')
        self.api.enable(admin)
        own = self.archive(admin, 'private-admin').json()
        self.private_mode()
        claimed = self.api.claim(['archive']).json()['job']
        self.assertEqual(claimed['id'], own['id'])
        with self.api.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?', (old['id'],)).fetchone()[0], 'queued')
