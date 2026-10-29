"""真实候选读取边界的范围提示：新旧分页均从可信来源目录派生。"""
import json
import unittest
from fastapi.testclient import TestClient
from highscore_web.app import create_app
import test_web_api

BASE = test_web_api.BASE


class CandidateScopeApiTests(unittest.TestCase):
    def setUp(self):
        self.h = test_web_api.WebApiTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        settings = {'public_url': 'http://testserver/highscore/',
                    'worker_token': 'synthetic-test-worker-secret-123456',
                    'secure_cookie': False, 'database_admin_only': False,
                    'database_source_types': [
                        {'database_id': 'synthetic-db', 'source': 'Synthetic index', 'reader_profile': 'jade_pdf4_2009_index_v1'},
                        {'database_id': 'synthetic-db', 'source': 'Synthetic full', 'reader_profile': 'pdf2_2.3'}]}
        self.h.client.close()
        self.h.client = TestClient(create_app(self.h.root, settings))
        self.addCleanup(self.h.client.close)
        self.user = self.h.register()
        self.h.enable(self.user)

    def test_historical_snapshots_receive_scope_without_rewriting_them_or_charging(self):
        response = self.h.client.post(BASE+'/jobs/search', headers={'X-CSRF-Token': self.user['csrf_token']},
            json={'mode': 'formula', 'query': 'V2O5', 'request_key': 'scope', 'search_version': 2})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json(); claim = self.h.claim().json()
        rows = [{'code': '09-999-0001', 'phase_name': 'Synthetic', 'source': 'Synthetic index',
                 'chemical_formula': 'V2O5', 'peak_count': 199, 'content_type': 'diffraction', 'exportable': True},
                {'code': '09-999-0002', 'phase_name': 'Synthetic', 'source': 'Synthetic full',
                 'chemical_formula': 'V2O5', 'peak_count': 62, 'space_group': 'Pmmn', 'content_type': 'diffraction'}]
        self.assertEqual(self.h.complete(claim, result={'candidates': rows}).status_code, 200)
        with self.h.db() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM candidate_details ORDER BY candidate_id')]
        wallet = self.h.client.get(BASE+'/credits').json()
        for route in (BASE+'/jobs/'+job['id'], BASE+'/jobs/'+job['id']+'/candidates'):
            response = self.h.client.get(route)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json(); items = result.get('items', result.get('candidates'))
            self.assertEqual(items[0]['report_type_label'], '索引报告')
            self.assertIn('尚未读取', items[0]['report_scope_detail'])
            self.assertTrue(items[0]['possible_truncation'])
            self.assertEqual(items[1]['report_type_label'], '数据库报告')
            self.assertEqual(items[1]['space_group'], 'Pmmn')
        self.assertEqual(wallet, self.h.client.get(BASE+'/credits').json())
        with self.h.db() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM candidate_details ORDER BY candidate_id')])

    def test_source_names_are_not_used_to_guess_reader_type(self):
        response = self.h.search(self.user)
        claim = self.h.claim().json()
        self.assertEqual(self.h.complete(claim, result={'candidates': [{'code': '09-999-0001',
            'phase_name': 'Synthetic', 'source': 'PDF-4+ 索引 unbound'}]}).status_code, 200)
        item = self.h.client.get(BASE+'/jobs/'+response.json()['id']).json()['candidates'][0]
        self.assertNotEqual(item.get('report_type_label'), '索引报告')


if __name__ == '__main__':
    unittest.main()
