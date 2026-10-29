"""文献与积分真实数据库/API回归；仅使用 synthetic PDF，不连接第三方。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from highscore_web import create_app

ROOT = Path(__file__).resolve().parents[1]
BASE = '/highscore/api'
TITLE = 'Synthetic literature request for integration verification'


def sample_pdf(title=TITLE,footer=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    content=f'BT /F1 12 Tf 20 750 Td ({title}) Tj ET'
    if footer:content+=f'\nBT /F1 8 Tf 20 30 Td ({footer}) Tj ET'
    stream.set_data(content.encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class LiteratureApiTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tmp'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache, prefix='literature-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, {'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
                                          'worker_token': 'synthetic-literature-worker-token-123456',
                                          'literature_worker_token': 'synthetic-private-literature-token-123456',
                                          'literature_mail_worker_token': 'synthetic-private-literature-mail-token-123456'})
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'X-CSRF-Token': 'synthetic-csrf'}
        self.worker = {'Authorization': 'Bearer synthetic-private-literature-token-123456'}
        self.mail_worker = {'Authorization': 'Bearer synthetic-private-literature-mail-token-123456'}
        with self.app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u1','synthetic@example.test','unused',1,1,?)", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(b'synthetic-session').hexdigest(), 'u1', 'synthetic-csrf', time.time()+86400))
        self.client.cookies.set('highscore_session', 'synthetic-session')

    def enable(self):
        response = self.client.patch(BASE+'/admin/literature', headers=self.headers, json={'enabled': True})
        self.assertEqual(response.status_code, 200, response.text)

    def credits(self):
        response = self.client.get(BASE+'/credits')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, title=TITLE, key='synthetic-key'):
        return self.client.post(BASE+'/literature/jobs', headers=self.headers, json={'title': title, 'request_key': key})

    def claim(self):
        response = self.client.post(BASE+'/worker/literature/claim', headers=self.worker, json={'worker_id': 'synthetic-literature', 'accounts_ready': True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, claim, **values):
        return self.client.post(BASE+f"/worker/literature/jobs/{claim['job']['id']}/update", headers=self.worker,
                                json={'lease_token': claim['lease_token'], **values})

    def test_registration_grant_once_and_daily_checkin_once_under_concurrency(self):
        self.assertEqual(self.credits()['available'], 10)
        self.assertEqual(self.credits()['available'], 10)
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.client.post(BASE+'/credits/checkin', headers=self.headers, json={}), range(6)))
        self.assertTrue(all(item.status_code == 200 for item in replies))
        wallet = self.credits()
        self.assertEqual((wallet['available'], wallet['frozen']), (11, 0))
        self.assertTrue(wallet['checked_in'])
        self.assertEqual(len([item for item in wallet['items'] if item['kind'] == 'checkin']), 1)

    def test_literature_has_independent_gate_and_no_daily_count_limit(self):
        self.assertEqual(self.submit().status_code, 503)
        self.enable()
        for number in range(22):
            response = self.submit(TITLE+f' {number}', str(number))
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            cancel = self.client.post(BASE+f"/literature/jobs/{job['id']}/cancel", headers=self.headers, json={})
            self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(self.credits()['available'], 10)
        self.assertFalse(self.client.get(BASE+'/health').json()['service_enabled'])

    def test_title_and_request_idempotency_do_not_double_freeze(self):
        self.enable()
        first = self.submit().json()
        self.assertEqual(self.submit().json()['id'], first['id'])
        self.assertEqual(self.submit(key='another-key').json()['id'], first['id'])
        self.assertEqual(self.submit(TITLE+' different', key='synthetic-key').status_code, 409)
        self.assertEqual((self.credits()['available'], self.credits()['frozen']), (9, 1))

    def test_user_task_views_hide_provider_and_account_diagnostics(self):
        self.enable()
        diagnostics = ('科研通账号暂不可用；原账号 synthetic-private-account；'
                       'https://www.ablesci.com/?token=synthetic-private-token；'
                       '正在扫描游标，Cookie与加密配置需要检查。')
        messages = {
            'waiting': '正在检索文献并等待全文，请稍后查看。',
            'needs_attention': '文献暂时无法获取，请稍后查看或联系管理员。',
            'needs_selection': '找到多个匹配结果，请确认需要的论文。',
            'failed': '文献求助未完成，冻结积分已退回。',
        }
        for index, (state, expected) in enumerate(messages.items()):
            with self.subTest(state=state):
                title = TITLE + ' visibility ' + str(index)
                job = self.submit(title, 'visibility-' + str(index)).json()
                claim = self.claim()
                values = {'state': state, 'message': diagnostics,
                          'account_ref': 'synthetic-private-account',
                          'external_request_id': 'synthetic-private-request'}
                if state == 'needs_selection':
                    values['candidates'] = [{'id': 'synthetic-choice', 'title': title, 'doi': None}]
                response = self.update(claim, **values)
                self.assertEqual(response.status_code, 200, response.text)
                detail = self.client.get(BASE + '/literature/jobs/' + job['id']).json()
                listed = next(item for item in self.client.get(BASE + '/literature/jobs').json()['items'] if item['id'] == job['id'])
                for public in (response.json(), detail, listed):
                    self.assertEqual(public['message'], expected)
                    self.assertEqual((public['state'], public['title']), (state, title))
                    self.assertNotIn('account_ref', public)
                    self.assertNotIn('external_request_id', public)
                    self.assertNotIn('synthetic-private', json.dumps(public))
                self.client.post(BASE + '/literature/jobs/' + job['id'] + '/cancel', headers=self.headers, json={})
        self.assertEqual((self.credits()['available'], self.credits()['frozen']), (10, 0))

    def test_historical_internal_message_is_hidden_without_rewriting_task(self):
        self.enable()
        job = self.submit().json()
        original = '科研通旧账号 synthetic-private-history 的会话与扫描游标需要核查。'
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET message=?,account_ref=?,external_request_id=? WHERE id=?',
                       (original, 'synthetic-private-account', 'synthetic-private-request', job['id']))
            before = dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (job['id'],)).fetchone())
        detail = self.client.get(BASE + '/literature/jobs/' + job['id']).json()
        listed = self.client.get(BASE + '/literature/jobs').json()['items'][0]
        for public in (detail, listed):
            self.assertEqual(public['message'], '已排队，正在等待处理。')
            self.assertNotIn('科研通', json.dumps(public, ensure_ascii=False))
            self.assertNotIn('synthetic-private', json.dumps(public))
        with self.app.state.database.connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (job['id'],)).fetchone()), before)
        self.assertEqual((self.credits()['available'], self.credits()['frozen']), (9, 1))

    def test_timeout_refunds_and_rejects_late_completion(self):
        self.enable()
        job = self.submit().json()
        claim = self.claim()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET deadline=? WHERE id=?', (time.time()-1, job['id']))
        current = self.client.get(BASE+f"/literature/jobs/{job['id']}").json()
        self.assertEqual((current['state'], current['credit_status']), ('failed', 'refunded'))
        self.assertEqual(self.credits()['available'], 10)
        response = self.update(claim, state='succeeded', pdf_base64=base64.b64encode(sample_pdf()).decode())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.credits()['frozen'], 0)

    def test_success_verified_pdf_charged_once_download_expiry_and_new_job(self):
        self.enable()
        job = self.submit().json()
        claim = self.claim()
        payload = sample_pdf()
        response = self.update(claim, state='succeeded', canonical_title=TITLE, pdf_base64=base64.b64encode(payload).decode())
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get(BASE+f"/literature/jobs/{job['id']}").json()
        self.assertEqual(current['credit_status'], 'charged')
        self.assertTrue(current['download_available'])
        for _ in range(2):
            downloaded = self.client.get(BASE+f"/literature/jobs/{job['id']}/download")
            self.assertEqual(downloaded.content, payload)
        self.assertEqual(self.credits()['available'], 9)
        self.assertEqual(self.submit(key='within-3-days').json()['id'], job['id'])
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET download_expires_at=? WHERE id=?', (time.time()-1, job['id']))
        self.assertEqual(self.client.get(BASE+f"/literature/jobs/{job['id']}/download").status_code, 410)
        expired = self.client.get(BASE+f"/literature/jobs/{job['id']}").json()
        self.assertTrue(expired['file_expired'])
        new = self.submit(key='after-expiry').json()
        self.assertNotEqual(new['id'], job['id'])
        self.assertEqual((self.credits()['available'], self.credits()['frozen']), (8, 1))

    def test_download_reminder_is_queued_only_after_verified_success_and_has_separate_auth(self):
        self.enable()
        job = self.submit().json()
        claim = self.claim()
        bad = self.update(claim,state='succeeded',pdf_base64=base64.b64encode(sample_pdf('Different paper')).decode())
        self.assertEqual(bad.status_code,422)
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM literature_reminders').fetchone()[0],0)
        payload = sample_pdf()
        success = self.update(claim,state='succeeded',canonical_title=TITLE,
                              pdf_base64=base64.b64encode(payload).decode())
        self.assertEqual(success.status_code,200,success.text)
        with self.app.state.database.connection() as db:
            reminder = db.execute('SELECT * FROM literature_reminders WHERE job_id=?',(job['id'],)).fetchone()
            task = db.execute('SELECT state,credit_status FROM literature_jobs WHERE id=?',(job['id'],)).fetchone()
        self.assertEqual(reminder['state'],'queued')
        self.assertEqual((task['state'],task['credit_status']),('succeeded','charged'))

        unauthorized = self.client.post(BASE+'/worker/literature/reminders/claim',json={'worker_id':'synthetic-mail'})
        self.assertEqual(unauthorized.status_code,401)
        claimed = self.client.post(BASE+'/worker/literature/reminders/claim',headers=self.mail_worker,
                                   json={'worker_id':'synthetic-literature-mail'})
        self.assertEqual(claimed.status_code,200,claimed.text)
        item = claimed.json()['reminder']
        self.assertEqual((item['task_id'],item['recipient']),(job['id'],'synthetic@example.test'))
        complete_path=BASE+f"/worker/literature/reminders/{item['id']}/complete"
        sent=self.client.post(complete_path,headers=self.mail_worker,
                             json={'lease_token':claimed.json()['lease_token'],'state':'submitted'})
        self.assertEqual(sent.status_code,200,sent.text)
        self.assertEqual(self.client.post(complete_path,headers=self.mail_worker,
                             json={'lease_token':claimed.json()['lease_token'],'state':'submitted'}).json()['state'],'submitted')
        self.assertIsNone(self.client.post(BASE+'/worker/literature/reminders/claim',headers=self.mail_worker,
                             json={'worker_id':'synthetic-literature-mail'}).json()['reminder'])
        self.assertEqual(self.credits()['available'],9)
        self.assertEqual(self.client.get(BASE+f"/literature/jobs/{job['id']}").json()['state'],'succeeded')

    def test_uncertain_expired_reminder_lease_is_not_requeued(self):
        self.enable()
        job=self.submit().json()
        success=self.update(self.claim(),state='succeeded',canonical_title=TITLE,
                            pdf_base64=base64.b64encode(sample_pdf()).decode())
        self.assertEqual(success.status_code,200,success.text)
        claimed=self.client.post(BASE+'/worker/literature/reminders/claim',headers=self.mail_worker,
                                 json={'worker_id':'synthetic-literature-mail'}).json()
        self.assertIsNotNone(claimed['reminder'])
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_reminders SET lease_until=? WHERE job_id=?',(time.time()-1,job['id']))
        next_claim=self.client.post(BASE+'/worker/literature/reminders/claim',headers=self.mail_worker,
                                    json={'worker_id':'synthetic-literature-mail'}).json()
        self.assertIsNone(next_claim['reminder'])
        with self.app.state.database.connection() as db:
            state=db.execute('SELECT state FROM literature_reminders WHERE job_id=?',(job['id'],)).fetchone()['state']
        self.assertEqual(state,'unknown')
        self.assertEqual(self.credits()['available'],9)

    def test_explicit_old_success_reminder_backfill_is_idempotent_and_free(self):
        self.enable()
        job=self.submit().json()
        completed=self.update(self.claim(),state='succeeded',canonical_title=TITLE,
                              pdf_base64=base64.b64encode(sample_pdf()).decode())
        self.assertEqual(completed.status_code,200,completed.text)
        with self.app.state.database.connection(write=True) as db:
            db.execute('DELETE FROM literature_reminders WHERE job_id=?',(job['id'],))
            first=self.app.state.literature.queue_existing_success_reminder(db,job['id'])
            second=self.app.state.literature.queue_existing_success_reminder(db,job['id'])
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(first['id'],second['id'])
        self.assertEqual((self.credits()['available'],self.credits()['frozen']),(9,0))

    def test_reject_wrong_pdf_and_isolate_user_downloads(self):
        self.enable()
        job = self.submit().json()
        claim = self.claim()
        bad = self.update(claim, state='succeeded', pdf_base64=base64.b64encode(sample_pdf('Wrong unrelated document')).decode())
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.credits()['available'], 9)
        with self.app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u2','other@example.test','unused',1,0,?)", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(b'other-session').hexdigest(), 'u2', 'other-csrf', time.time()+86400))
        self.client.cookies.set('highscore_session', 'other-session')
        self.assertEqual(self.client.get(BASE+f"/literature/jobs/{job['id']}").status_code, 404)
        self.assertEqual(self.client.get(BASE+f"/literature/jobs/{job['id']}/download").status_code, 404)

    def test_browser_worker_auth_and_stale_lease(self):
        self.enable()
        self.submit()
        self.assertEqual(self.client.post(BASE+'/worker/literature/claim', json={'worker_id':'bad','accounts_ready':True}).status_code, 401)
        first = self.claim()
        self.assertIsNone(self.claim()['job'])
        self.assertEqual(self.update(first, state='waiting', external_request_id='synthetic-req', message='等待应助').status_code, 200)
        self.assertEqual(self.update(first, state='failed').status_code, 409)

    def test_metadata_retry_does_not_turn_raw_input_into_verified_canonical_title(self):
        self.enable()
        job=self.submit().json()
        response=self.update(self.claim(),state='waiting',message='权威元数据暂时无法核验')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['canonical_title'])
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET next_poll=0 WHERE id=?',(job['id'],))
        self.assertIsNone(self.claim()['job']['canonical_title'])

    def test_xrd_admin_contract_remains_separate(self):
        self.enable()
        settings = self.client.get(BASE+'/admin/status').json()['settings']
        self.assertNotIn('literature_enabled', settings)
        self.assertEqual(set(settings), {'license_confirmed','cloud_validated','public_enabled','search_daily_limit','archive_daily_limit'})

    def test_frozen_balance_blocks_overspending_and_cancellation_restores(self):
        self.enable()
        jobs = [self.submit(TITLE+f' unique {number}',str(number)).json() for number in range(10)]
        self.assertEqual((self.credits()['available'],self.credits()['frozen']), (0,10))
        self.assertEqual(self.submit(TITLE+' too much','extra').status_code,402)
        for _ in range(2):
            self.client.post(BASE+f"/literature/jobs/{jobs[0]['id']}/cancel",headers=self.headers,json={})
        self.assertEqual((self.credits()['available'],self.credits()['frozen']), (1,9))

    def test_candidate_selection_is_bound_and_deadline_is_not_reset(self):
        self.enable()
        job = self.submit().json()
        claim = self.claim()
        response = self.update(claim,state='needs_selection',candidates=[{'id':'synthetic-candidate','title':TITLE+' variant','doi':None}])
        self.assertEqual(response.status_code,200,response.text)
        bad = self.client.post(BASE+f"/literature/jobs/{job['id']}/select",headers=self.headers,json={'candidate_id':'not-owned'})
        self.assertEqual(bad.status_code,404)
        selected = self.client.post(BASE+f"/literature/jobs/{job['id']}/select",headers=self.headers,json={'candidate_id':'synthetic-candidate'})
        self.assertEqual(selected.status_code,200,selected.text)
        self.assertEqual(selected.json()['deadline'],job['deadline'])
        self.assertEqual(self.credits()['frozen'],1)

    def test_reference_cannot_establish_pdf_identity(self):
        from highscore_literature.pdf import validate_pdf
        quoted=sample_pdf('A completely different article\nReferences: '+TITLE+' DOI 10.1234/requested.2026')
        with self.assertRaises(ValueError):validate_pdf(quoted,TITLE,'10.1234/requested.2026')
        abstract=sample_pdf('A completely different article\nAbstract\nWe discuss '+TITLE+' DOI 10.1234/requested.2026')
        with self.assertRaises(ValueError):validate_pdf(abstract,TITLE,'10.1234/requested.2026')

    def test_pdf_doi_must_match_complete_publication_identifier(self):
        from highscore_literature.pdf import validate_pdf
        requested='10.1234/requested'
        self.assertTrue(validate_pdf(sample_pdf(TITLE+'\nDOI: '+requested+'\nAbstract\nStudy text'),TITLE,requested))
        self.assertTrue(validate_pdf(sample_pdf(TITLE+'\nAbstract\nStudy text',footer='https://doi.org/'+requested),TITLE,requested))
        for content in (
            TITLE+'\nDOI: '+requested+'2\nAbstract\nStudy text',
            TITLE+'\nDOI: 10.1234/another\nAbstract\nStudy text\nReferences\n'+requested,
            TITLE+'\nAbstract\nThis paper cites DOI '+requested+' in its summary',
            TITLE+'\nAbstract\nStudy text\nReferences\nhttps://doi.org/'+requested,
            TITLE+'\nAbstract\nDOI: '+requested+' is discussed as a previous study',
            TITLE+'\nAbstract\nDOI: '+requested,
            TITLE+'\nReferences and Notes\nhttps://doi.org/'+requested,
        ):
            with self.subTest(content=content),self.assertRaises(ValueError):
                validate_pdf(sample_pdf(content),TITLE,requested)
        for heading in ('References','References and Notes','Bibliography'):
            with self.subTest(footer_after=heading),self.assertRaises(ValueError):
                validate_pdf(sample_pdf(TITLE+'\nAbstract\nStudy text\n'+heading,footer='https://doi.org/'+requested),TITLE,requested)

    def test_pdf_accepts_nature_article_in_press_publication_header(self):
        from highscore_literature.pdf import publication_dois, validate_pdf
        requested='10.1038/s41467-026-77390-7'
        title='Biocompatible piezoionic eutectogel sensor for self-powered physiological monitoring'
        for identifier in (requested,'https://doi.org/'+requested):
            header='Article | Article in Press '+identifier
            with self.subTest(identifier=identifier):
                self.assertEqual(publication_dois(header),{requested})
                content=header+'\nBiocompatible piezoionic eutectogel sensor\nfor self-powered physiological monitoring\nAbstract\nStudy text'
                self.assertTrue(validate_pdf(sample_pdf(content),title,requested))

    def test_pdf_nature_article_in_press_header_keeps_strict_identity_boundaries(self):
        from highscore_literature.pdf import publication_dois, validate_pdf
        requested='10.1038/s41467-026-77390-7'
        header='Article | Article in Press '
        for identifier in ('10.1038/s41467-026-77390-8',requested+'2'):
            with self.subTest(wrong_identifier=identifier),self.assertRaises(ValueError):
                validate_pdf(sample_pdf(header+'https://doi.org/'+identifier+'\n'+TITLE+'\nAbstract\nStudy text'),TITLE,requested)
        for line in (
            header+'https://doi.org/'+requested+' is discussed as a previous study',
            header+'https://doi.org/'+requested+' https://doi.org/10.1038/s41467-026-77390-8',
            header+'https://doi.org/'+requested+' https://doi.org/'+requested,
            'We cite '+header+'https://doi.org/'+requested,
            'Article | Article '+requested,
            'Article in Press https://doi.org/'+requested,
            'Article | Article in Press: https://doi.org/'+requested,
        ):
            with self.subTest(invalid_header=line):
                self.assertEqual(publication_dois(line),set())
                with self.assertRaises(ValueError):
                    validate_pdf(sample_pdf(line+'\n'+TITLE+'\nAbstract\nStudy text'),TITLE,requested)
        for heading in ('Abstract','Introduction','References','References and Notes','Bibliography'):
            with self.subTest(header_after=heading),self.assertRaises(ValueError):
                validate_pdf(sample_pdf(TITLE+'\n'+heading+'\n'+header+'https://doi.org/'+requested),TITLE,requested)

    def test_literature_rejects_xrd_worker_credential(self):
        denied=self.client.post(BASE+'/worker/literature/claim',headers={'Authorization':'Bearer synthetic-literature-worker-token-123456'},json={'worker_id':'old-worker','accounts_ready':True})
        self.assertEqual(denied.status_code,401)

    def test_late_callback_itself_commits_timeout_refund(self):
        self.enable()
        job=self.submit().json()
        claim=self.claim()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET deadline=? WHERE id=?',(time.time()-1,job['id']))
        response=self.update(claim,state='failed')
        self.assertEqual(response.status_code,409,response.text)
        with self.app.state.database.connection() as db:
            row=db.execute('SELECT state,credit_status FROM literature_jobs WHERE id=?',(job['id'],)).fetchone()
        self.assertEqual(tuple(row),('failed','refunded'))

    def test_pdf_verification_crossing_deadline_does_not_charge(self):
        self.enable()
        job=self.submit().json()
        claim=self.claim()
        now=time.time()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET deadline=? WHERE id=?',(now+5,job['id']))
        from highscore_literature.pdf import validate_pdf
        clock=[now]
        def slow_check(*args):
            proof=validate_pdf(*args)
            clock[0]=now+6
            return proof
        with patch('highscore_web.literature_store.time.time',side_effect=lambda:clock[0]),patch('highscore_web.literature_store.validate_pdf',side_effect=slow_check):
            response=self.update(claim,state='succeeded',pdf_base64=base64.b64encode(sample_pdf()).decode())
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual((self.credits()['available'],self.credits()['frozen']),(10,0))

    def test_upload_rejects_symlink_ancestor_before_charge(self):
        self.enable()
        self.submit()
        claim=self.claim()
        original=Path.is_symlink
        def symlink(path):return path==self.root/'output/literature' or original(path)
        with patch.object(Path,'is_symlink',symlink):
            response=self.update(claim,state='succeeded',pdf_base64=base64.b64encode(sample_pdf()).decode())
        self.assertEqual(response.status_code,410,response.text)
        self.assertEqual((self.credits()['available'],self.credits()['frozen']),(9,1))

    def test_selected_candidate_without_doi_keeps_source_identity(self):
        self.enable()
        job=self.submit().json()
        candidates=[{'id':'record-1','title':TITLE,'journal':'Synthetic journal A','year':2025},
                    {'id':'record-2','title':TITLE,'journal':'Synthetic journal B','year':2026}]
        self.assertEqual(self.update(self.claim(),state='needs_selection',candidates=candidates).status_code,200)
        response=self.client.post(BASE+f"/literature/jobs/{job['id']}/select",headers=self.headers,json={'candidate_id':'record-2'})
        self.assertEqual(response.status_code,200,response.text)
        claimed=self.claim()['job']
        self.assertEqual(claimed['selected_candidate'],{**candidates[1],'doi':None})
        self.assertEqual(response.json()['deadline'],job['deadline'])

    def test_slow_pdf_validation_does_not_lock_other_users_and_cancel_wins(self):
        self.enable()
        job=self.submit().json()
        claim=self.claim()
        started,release=threading.Event(),threading.Event()
        from highscore_literature.pdf import validate_pdf
        def slow_check(*args):
            started.set()
            if not release.wait(8):raise AssertionError('解析未被释放')
            return validate_pdf(*args)
        with patch('highscore_web.literature_store.validate_pdf',side_effect=slow_check),ThreadPoolExecutor(max_workers=3) as pool:
            upload=pool.submit(self.update,claim,state='succeeded',pdf_base64=base64.b64encode(sample_pdf()).decode())
            self.assertTrue(started.wait(3))
            balance=pool.submit(self.credits)
            try:
                self.assertEqual(balance.result(timeout=2)['frozen'],1)
                cancelled=self.client.post(BASE+f"/literature/jobs/{job['id']}/cancel",headers=self.headers,json={})
                self.assertEqual(cancelled.status_code,200,cancelled.text)
            finally:release.set()
            result=upload.result(timeout=5)
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual((self.credits()['available'],self.credits()['frozen']),(10,0))
        self.assertFalse((self.root/f"output/literature/{job['id']}/fulltext.pdf").exists())

    def test_same_size_corruption_stops_reuse_and_has_clear_recovery(self):
        self.enable()
        job=self.submit().json()
        self.assertEqual(self.update(self.claim(),state='succeeded',pdf_base64=base64.b64encode(sample_pdf()).decode()).status_code,200)
        path=self.root/f"output/literature/{job['id']}/fulltext.pdf"
        data=path.read_bytes()
        path.write_bytes(data[:-1]+bytes([data[-1]^1]))
        current=self.client.get(BASE+f"/literature/jobs/{job['id']}").json()
        self.assertFalse(current['download_available'])
        self.assertIn('重新提交',current['message'])
        self.assertEqual(self.client.get(BASE+f"/literature/jobs/{job['id']}/download").status_code,410)
        self.assertNotEqual(self.submit(key='after-corruption').json()['id'],job['id'])

    def test_heartbeat_refreshes_worker_availability(self):
        self.enable()
        self.submit()
        claim=self.claim()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_workers SET last_seen=?',(time.time()-100,))
        self.assertFalse(self.client.get(BASE+'/literature/status').json()['worker_online'])
        heartbeat=self.client.post(BASE+f"/worker/literature/jobs/{claim['job']['id']}/heartbeat",headers=self.worker,json={'lease_token':claim['lease_token']})
        self.assertEqual(heartbeat.status_code,200,heartbeat.text)
        self.assertTrue(self.client.get(BASE+'/literature/status').json()['worker_online'])


if __name__ == '__main__':
    unittest.main()
