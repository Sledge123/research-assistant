"""ICSD独立编号和内容检索均沿原账号/任务/积分链路。"""
import threading
import unittest
from unittest.mock import patch

from highscore_downloader.numbers import parse_code,parse_codes
from highscore_database.reader import OrderedDatabaseReader,DatabaseError
from highscore_database.executor import JobHandler
import test_database_executor
import test_web_api


class StructureNumberTests(unittest.TestCase):
    def test_accession_is_normalized_without_colliding_with_pdf_numbers(self):
        code=parse_code('icsd-12')
        self.assertEqual(code.canonical_hint,'ICSD-000012')
        self.assertTrue(code.matches('ICSD-12'))
        self.assertFalse(code.matches('00-000-0012'))
        self.assertFalse(parse_code('54-12').matches('ICSD-000012'))
        self.assertEqual(len(parse_codes('ICSD-12 icsd-000012 00-000-0012')),2)
        for value in ('ICSD-0','ICSD-1000000','ICSD-../1','ICSD-1.cif'):
            with self.assertRaises(ValueError):parse_code(value)


class ContentReaderTests(unittest.TestCase):
    def setUp(self):test_database_executor.ExecutorTests.setUp(self)
    def job(self,**values):return test_database_executor.ExecutorTests.job(self,**values)
    def test_structure_search_never_uses_diffraction_match_for_same_formula(self):
        second=type('Structure',(),{'database_id':'jadestr-'+'a'*48,'sha256':'a'*64,
            'source':'Synthetic structure','content_type':'structure','stop_event':threading.Event()})()
        second.search=lambda mode,query:[{'code':'ICSD-000012','phase_name':'Synthetic','source':second.source}]
        reader=OrderedDatabaseReader([self.reader,second])
        with patch.object(self.reader,'search',side_effect=AssertionError('diffraction query forbidden')):
            self.assertEqual(reader.search_content('formula','X Y','structure')[0]['code'],'ICSD-000012')
        with patch.object(second,'search',side_effect=AssertionError('structure query forbidden')):
            self.assertEqual(reader.search('formula','X Y')[0]['code'],'09-999-0001')
        with self.assertRaises(DatabaseError):reader.search_content('formula','X Y','database')
        with self.assertRaises(DatabaseError):OrderedDatabaseReader([self.reader]).search_content('formula','X Y','structure')


class StructureApiTests(unittest.TestCase):
    def setUp(self):
        self.h=test_web_api.WebApiTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.user=self.h.register();self.h.enable(self.user)
        self.headers={'X-CSRF-Token':self.user['csrf_token']}

    def test_structure_search_preserves_content_and_selected_real_accession(self):
        response=self.h.client.post('/highscore/api/jobs/search',headers=self.headers,
            json={'mode':'formula','query':'X Y','content':'structure','request_key':'structure-search'})
        self.assertEqual(response.status_code,200,response.text)
        claimed=self.h.claim().json()
        self.assertEqual(claimed['job']['payload']['content'],'structure')
        done=self.h.complete(claimed,result={'candidates':[{'code':'ICSD-000012','phase_name':'Synthetic structure',
            'chemical_formula':'X Y','source':'Synthetic ICSD'}]})
        self.assertEqual(done.status_code,200,done.text)
        job=self.h.client.get('/highscore/api/jobs/'+claimed['job']['id']).json()
        selected=self.h.client.post('/highscore/api/jobs/archive',headers=self.headers,json={
            'search_job_id':job['id'],'candidate_ids':[job['candidates'][0]['id']],
            'request_key':'structure-select','send_mail':False})
        self.assertEqual(selected.status_code,200,selected.text)
        archive=self.h.claim().json()['job']
        self.assertEqual(archive['payload']['selected'][0]['code'],'ICSD-000012')
        self.assertEqual(archive['payload']['selected'][0]['source'],'Synthetic ICSD')
        self.assertEqual(selected.json()['credits']['reserved'],1)

    def test_invalid_content_is_rejected_and_search_is_free(self):
        before=self.h.client.get('/highscore/api/credits').json()
        response=self.h.client.post('/highscore/api/jobs/search',headers=self.headers,
            json={'mode':'formula','query':'X Y','content':'secret-database','request_key':'bad-content'})
        self.assertEqual(response.status_code,422)
        after=self.h.client.get('/highscore/api/credits').json()
        self.assertEqual((before['available'],before['frozen']),(after['available'],after['frozen']))


if __name__=='__main__':unittest.main()
