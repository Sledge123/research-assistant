"""数据库选择必须绑定任务与领取者，不能随机落到另一库。"""
import unittest

from fastapi.testclient import TestClient
from highscore_web import create_app
import test_web_api

BASE = '/highscore/api'


class DatabaseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.api = test_web_api.WebApiTests()
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        app = create_app(self.api.root, {'public_url': 'http://testserver/highscore/',
                         'worker_token': 'synthetic-test-worker-secret-123456', 'secure_cookie': False,
                         'database_catalog': [{'id': 'synthetic-pdf', 'name': 'PDF-2'}, {'id': 'synthetic-cod', 'name': 'COD 2024'}]})
        self.api.client = TestClient(app)
        self.addCleanup(self.api.client.close)
        self.user = self.api.register()
        self.api.enable(self.user)
        self.headers = {'X-CSRF-Token': self.user['csrf_token']}

    def claim(self, identity):
        return self.api.client.post(BASE + '/worker/claim', headers=self.api.worker,
                    json={'worker_id': identity, 'database_id': identity, 'capabilities': ['search', 'archive']}).json()

    def test_catalog_contains_only_configured_public_labels(self):
        response = self.api.client.get(BASE + '/databases')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['default_id'], 'synthetic-pdf')
        self.assertEqual([r['name'] for r in response.json()['items']], ['PDF-2', 'COD 2024'])

    def test_cod_search_cannot_be_claimed_by_pdf_worker(self):
        response = self.api.client.post(BASE + '/jobs/search', headers=self.headers,
                 json={'mode': 'mineral_name', 'query': 'Zincite', 'database_id': 'synthetic-cod', 'request_key': 'cod-search'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.claim('synthetic-pdf')['job'])
        job = self.claim('synthetic-cod')['job']
        self.assertEqual(job['id'], response.json()['id'])
        self.assertEqual(job['database_id'], 'synthetic-cod')

    def test_default_archive_stays_pdf_and_unknown_database_is_rejected(self):
        body = {'codes': '30-0820', 'send_mail': False, 'request_key': 'default-pdf'}
        response = self.api.client.post(BASE + '/jobs/archive', headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.claim('synthetic-cod')['job'])
        self.assertEqual(self.claim('synthetic-pdf')['job']['database_id'], 'synthetic-pdf')
        response = self.api.client.post(BASE + '/jobs/archive', headers=self.headers,
                 json={**body, 'request_key': 'bad-db', 'database_id': 'not-configured'})
        self.assertEqual(response.status_code, 422)

    def test_selected_candidate_keeps_cod_source_and_database(self):
        response = self.api.client.post(BASE + '/jobs/search', headers=self.headers,
                 json={'mode': 'mineral_name', 'query': 'Zincite', 'database_id': 'synthetic-cod', 'request_key': 'selected-search'})
        self.assertEqual(response.status_code, 200, response.text)
        claimed = self.claim('synthetic-cod')
        done = self.api.complete(claimed, result={'candidates': [{'code': '96-101-1259', 'phase_name': 'Zincite', 'chemical_formula': 'Zn2 O2', 'source': 'COD 2024'}]})
        self.assertEqual(done.status_code, 200, done.text)
        search = self.api.client.get(BASE + '/jobs/' + claimed['job']['id']).json()
        response = self.api.client.post(BASE + '/jobs/archive', headers=self.headers,
                 json={'search_job_id': search['id'], 'candidate_ids': [search['candidates'][0]['id']], 'send_mail': False, 'request_key': 'cod-selection'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.claim('synthetic-pdf')['job'])
        claimed = self.claim('synthetic-cod')['job']
        self.assertEqual(claimed['payload']['selected'][0]['source'], 'COD 2024')
        self.assertEqual(claimed['database_id'], 'synthetic-cod')

    def automatic(self):
        app=create_app(self.api.root,{'public_url':'http://testserver/highscore/',
              'worker_token':'synthetic-test-worker-secret-123456','secure_cookie':False,
              'database_catalog':[{'id':'synthetic-auto','name':'自动检索'},
                                  {'id':'synthetic-pdf','name':'PDF-2'},
                                  {'id':'synthetic-cod','name':'COD 2024'}],
              'database_auto_id':'synthetic-auto'})
        self.api.client=TestClient(app);self.addCleanup(self.api.client.close)
        self.api.client.post(BASE+'/auth/login',json={'email':self.user['email'],'password':'synthetic-password-123'})
        self.headers={'X-CSRF-Token':self.api.client.get(BASE+'/auth/me').json()['csrf_token']}

    def test_automatic_jobs_bind_only_ordered_worker(self):
        self.automatic()
        response=self.api.client.post(BASE+'/jobs/search',headers=self.headers,
                 json={'mode':'compound_name','query':'Zincite','request_key':'automatic-search'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(self.claim('synthetic-pdf')['job'])
        self.assertIsNone(self.claim('synthetic-cod')['job'])
        self.assertEqual(self.claim('synthetic-auto')['job']['id'],response.json()['id'])

    def test_automatic_mode_rejects_manual_database_override(self):
        self.automatic()
        response=self.api.client.post(BASE+'/jobs/archive',headers=self.headers,
                 json={'codes':'30-0820','database_id':'synthetic-pdf','request_key':'manual-bypass'})
        self.assertEqual(response.status_code,422,response.text)

    def test_automatic_health_requires_automatic_worker(self):
        self.automatic();self.claim('synthetic-pdf')
        self.assertFalse(self.api.client.get(BASE+'/health').json()['worker_online'])
        self.claim('synthetic-auto')
        self.assertTrue(self.api.client.get(BASE+'/health').json()['worker_online'])
