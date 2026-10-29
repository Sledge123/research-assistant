"""来源接续与业务阶段的 HTTP/真实 SQLite 回归，全部使用离线合成数据。"""
import base64
import json
import time
import unittest

import test_literature_api as api_helpers

BASE = api_helpers.BASE
TITLE = api_helpers.TITLE
DOI = '10.1234/synthetic-source-paper'
ACCOUNT = 'synthetic-private-account'
OLD_REQUEST = 'synthetic-private-expired-request'
EVIDENCE = 'a' * 64


class LiteratureSourceApiTests(unittest.TestCase):
    # 只复用测试装配与 HTTP helper，不继承原类的全部用例。
    setUp = api_helpers.LiteratureApiTests.setUp
    enable = api_helpers.LiteratureApiTests.enable
    credits = api_helpers.LiteratureApiTests.credits
    submit = api_helpers.LiteratureApiTests.submit
    claim = api_helpers.LiteratureApiTests.claim
    update = api_helpers.LiteratureApiTests.update

    def bound(self, key='synthetic-key'):
        self.enable()
        job = self.submit(key=key).json()
        claim = self.claim()
        response = self.update(claim, state='waiting', canonical_title=TITLE, doi=DOI,
                               account_ref=ACCOUNT, external_request_id=OLD_REQUEST)
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET next_poll=0 WHERE id=?', (job['id'],))
        return job, self.claim()

    @staticmethod
    def reset_values(**changes):
        return {'state': 'waiting', 'source_revision': 0,
                'source_reset': {'previous_request_id': OLD_REQUEST, 'source_revision': 0,
                                 'reason': 'expired', 'evidence_sha256': EVIDENCE}, **changes}

    def detail(self, job):
        response = self.client.get(BASE + '/literature/jobs/' + job['id'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def snapshot(self, job):
        with self.app.state.database.connection() as db:
            row = dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (job['id'],)).fetchone())
            audit = [dict(value) for value in db.execute("SELECT * FROM audit WHERE action='literature_source_reset' ORDER BY id")]
        return row, audit, self.credits()

    def test_old_database_migrates_source_fields_without_changing_existing_job_or_credits(self):
        self.enable()
        job = self.submit().json()
        wallet = self.credits()
        with self.app.state.database.connection(write=True) as db:
            for name in ('source_revision', 'stage', 'last_checked_at'):
                db.execute('ALTER TABLE literature_jobs DROP COLUMN ' + name)
        from highscore_web.literature_store import LiteratureStore
        LiteratureStore(self.root, self.app.state.database)
        current = self.detail(job)
        self.assertEqual((current['id'], current['deadline'], current['credit_status']),
                         (job['id'], job['deadline'], job['credit_status']))
        self.assertEqual((current['stage'], current['last_checked_at'], current['next_check_at']), ('', None, None))
        self.assertEqual(current['message'], '已排队，正在等待处理。')
        self.assertEqual(self.claim()['job']['source_revision'], 0)
        self.assertEqual(self.credits(), wallet)

    def test_expired_source_continues_same_job_with_original_identity_credit_and_deadline(self):
        job, claim = self.bound()
        before = self.detail(job)
        wallet = self.credits()
        response = self.update(claim, **self.reset_values())
        self.assertEqual(response.status_code, 200, response.text)
        current = self.detail(job)
        for name in ('id', 'title', 'canonical_title', 'doi', 'created_at', 'deadline', 'credit_status'):
            self.assertEqual(current[name], before[name], name)
        self.assertEqual((current['state'], current['stage'], current['message']),
                         ('waiting', 'renewing', '正在重新获取'))
        self.assertEqual(self.credits(), wallet)
        continued = self.claim()['job']
        self.assertEqual((continued['id'], continued['account_ref'], continued['external_request_id'],
                          continued['source_revision'], continued['doi']),
                         (job['id'], ACCOUNT, None, 1, DOI))
        self.assertEqual(continued['created_at'], claim['job']['created_at'])
        self.assertEqual(continued['deadline'], claim['job']['deadline'])
        with self.app.state.database.connection() as db:
            entries = db.execute("SELECT detail FROM audit WHERE action='literature_source_reset'").fetchall()
        self.assertEqual(len(entries), 1)
        audit = json.loads(entries[0]['detail'])
        self.assertEqual((audit['job_id'], audit['worker_id'], audit['previous_request_id'],
                          audit['source_revision'], audit['reason'], audit['evidence_sha256']),
                         (job['id'], 'synthetic-literature', OLD_REQUEST, 0, 'expired', EVIDENCE))

    def test_heartbeat_reports_checked_stage_without_exposing_internal_diagnostics(self):
        job, claim = self.bound()
        response = self.client.post(BASE + '/worker/literature/jobs/' + job['id'] + '/heartbeat',
                                    headers=self.worker, json={'lease_token': claim['lease_token'],
                                                               'stage': 'awaiting_fulltext'})
        self.assertEqual(response.status_code, 200, response.text)
        current = self.detail(job)
        self.assertEqual((current['state'], current['stage'], current['message']),
                         ('running', 'awaiting_fulltext', '等待全文'))
        self.assertIsNotNone(current['last_checked_at'])
        self.assertIsNone(current['next_check_at'])
        for hidden in ('source_revision', 'source_reset', 'account_ref', 'external_request_id'):
            self.assertNotIn(hidden, current)
        self.assertNotIn('synthetic-private', json.dumps(current))

        # ready 只有服务器复验 PDF 成功后产生，不能被心跳或等待结果写入。
        with self.app.state.database.connection() as db:
            before = dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (job['id'],)).fetchone())
        denied = self.client.post(BASE + '/worker/literature/jobs/' + job['id'] + '/heartbeat',
                                  headers=self.worker, json={'lease_token': claim['lease_token'], 'stage': 'ready'})
        self.assertEqual(denied.status_code, 409, denied.text)
        denied = self.update(claim, state='waiting', stage='ready')
        self.assertEqual(denied.status_code, 409, denied.text)
        with self.app.state.database.connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (job['id'],)).fetchone()), before)

    def test_invalid_reset_revision_request_identity_and_payload_have_no_side_effects(self):
        job, claim = self.bound()
        reset = self.reset_values()['source_reset']
        invalid = [
            ({'source_revision': 1}, 409),
            ({'source_reset': {**reset, 'source_revision': 1}}, 409),
            ({'source_reset': {**reset, 'previous_request_id': 'wrong-request'}}, 409),
            ({'account_ref': 'wrong-account'}, 409),
            ({'external_request_id': 'replacement-request'}, 409),
            ({'doi': '10.1234/different-paper'}, 422),
            ({'canonical_title': 'A completely unrelated publication title'}, 422),
            ({'state': 'failed'}, 409),
            ({'pdf_base64': ''}, 409),
            ({'candidates': []}, 409),
            ({'source_reset': {**reset, 'reason': 'unknown'}}, 422),
            ({'source_reset': {**reset, 'evidence_sha256': 'A' * 64}}, 422),
            ({'source_revision': -1}, 422),
        ]
        before = self.snapshot(job)
        for changes, status in invalid:
            with self.subTest(changes=changes):
                response = self.update(claim, **self.reset_values(**changes))
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.snapshot(job), before)
        ordinary = self.update(claim, state='waiting', external_request_id='replacement-request')
        self.assertEqual(ordinary.status_code, 409, ordinary.text)
        self.assertEqual(self.snapshot(job), before)

    def test_source_reset_replay_and_missing_or_stale_revision_cannot_rebind_new_source(self):
        job, old_claim = self.bound()
        reset = self.update(old_claim, **self.reset_values())
        self.assertEqual(reset.status_code, 200, reset.text)
        after_reset = self.snapshot(job)
        replay = self.update(old_claim, **self.reset_values())
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(self.snapshot(job), after_reset)
        claim = self.claim()
        before = self.snapshot(job)
        for changes in ({}, {'source_revision': 0}, {'source_revision': 2}):
            with self.subTest(changes=changes):
                response = self.update(claim, state='waiting', external_request_id='new-request', **changes)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.snapshot(job), before)
        accepted = self.update(claim, state='waiting', source_revision=1, external_request_id='new-request',
                               account_ref=ACCOUNT, stage='awaiting_fulltext')
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['message'], '等待全文')
        self.assertIsNotNone(accepted.json()['next_check_at'])

    def test_renewed_verified_fulltext_charges_once_keeps_three_days_and_one_reminder(self):
        job, old_claim = self.bound()
        self.assertEqual(self.update(old_claim, **self.reset_values()).status_code, 200)
        claim = self.claim()
        payload = api_helpers.sample_pdf(TITLE, 'DOI: ' + DOI)
        values = {'state': 'succeeded', 'source_revision': 1, 'external_request_id': 'new-request',
                  'account_ref': ACCOUNT, 'pdf_base64': base64.b64encode(payload).decode()}
        succeeded = self.update(claim, **values)
        self.assertEqual(succeeded.status_code, 200, succeeded.text)
        current = self.detail(job)
        self.assertEqual((current['state'], current['stage'], current['message'], current['credit_status']),
                         ('succeeded', 'ready', '可下载', 'charged'))
        self.assertTrue(current['download_available'])
        self.assertEqual(current['deadline'], job['deadline'])
        from datetime import datetime
        self.assertEqual((datetime.fromisoformat(current['download_expires_at']) -
                          datetime.fromisoformat(current['ready_at'])).total_seconds(), 3 * 86400)
        before = self.snapshot(job)
        self.assertEqual(self.update(claim, **values).status_code, 409)
        self.assertEqual(self.update(old_claim, state='failed').status_code, 409)
        self.assertEqual(self.snapshot(job), before)
        for _ in range(2):
            downloaded = self.client.get(BASE + '/literature/jobs/' + job['id'] + '/download')
            self.assertEqual(downloaded.content, payload)
        wallet = self.credits()
        self.assertEqual((wallet['available'], wallet['frozen']), (9, 0))
        self.assertEqual(len([item for item in wallet['items'] if item['kind'] == 'charge']), 1)
        reminder = self.client.post(BASE + '/worker/literature/reminders/claim', headers=self.mail_worker,
                                    json={'worker_id': 'synthetic-mail'}).json()
        self.assertEqual(reminder['reminder']['task_id'], job['id'])
        complete = BASE + '/worker/literature/reminders/' + reminder['reminder']['id'] + '/complete'
        for _ in range(2):
            response = self.client.post(complete, headers=self.mail_worker,
                                        json={'lease_token': reminder['lease_token'], 'state': 'submitted'})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.client.post(BASE + '/worker/literature/reminders/claim', headers=self.mail_worker,
                                           json={'worker_id': 'synthetic-mail'}).json()['reminder'])
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM literature_reminders WHERE job_id=?', (job['id'],)).fetchone()[0], 1)

    def test_cancel_and_original_deadline_after_source_reset_refund_once_and_reject_callbacks(self):
        for ending in ('cancel', 'timeout'):
            with self.subTest(ending=ending):
                job, old_claim = self.bound(key='ending-' + ending)
                self.assertEqual(self.update(old_claim, **self.reset_values()).status_code, 200)
                claim = self.claim()
                if ending == 'cancel':
                    for _ in range(2):
                        response = self.client.post(BASE + '/literature/jobs/' + job['id'] + '/cancel',
                                                    headers=self.headers, json={})
                        self.assertEqual(response.status_code, 200, response.text)
                    expected = 'cancelled'
                else:
                    with self.app.state.database.connection(write=True) as db:
                        db.execute('UPDATE literature_jobs SET deadline=? WHERE id=?', (time.time() - 1, job['id']))
                    expected = 'failed'
                late = self.update(claim, state='waiting', source_revision=1, external_request_id='new-request')
                self.assertEqual(late.status_code, 409, late.text)
                current = self.detail(job)
                self.assertEqual((current['state'], current['credit_status']), (expected, 'refunded'))
                self.assertNotEqual(current['message'], '正在重新获取')
                wallet = self.credits()
                self.assertEqual((wallet['available'], wallet['frozen']), (10, 0))
                self.assertEqual(len([item for item in wallet['items'] if item['kind'] == 'refund' and item['job_id'] == job['id']]), 1)
                with self.app.state.database.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM literature_reminders WHERE job_id=?', (job['id'],)).fetchone()[0], 0)

    def test_stage_messages_are_fixed_and_business_failure_has_priority(self):
        job, claim = self.bound()
        for stage, message in (('fetching', '正在获取正文'), ('awaiting_fulltext', '等待全文'),
                               ('renewing', '正在重新获取'), ('verifying', '正在核验'), ('retrying', '稍后重试')):
            with self.subTest(stage=stage):
                response = self.client.post(BASE + '/worker/literature/jobs/' + job['id'] + '/heartbeat',
                                            headers=self.worker, json={'lease_token': claim['lease_token'], 'stage': stage})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.detail(job)['message'], message)
        response = self.update(claim, state='needs_attention', stage='fetching',
                               message='科研通账号 synthetic-private-account Cookie token diagnostic')
        self.assertEqual(response.status_code, 200, response.text)
        for current in (response.json(), self.detail(job), self.client.get(BASE + '/literature/jobs').json()['items'][0]):
            self.assertEqual(current['message'], '文献暂时无法获取，请稍后查看或联系管理员。')
            self.assertIsNotNone(current['next_check_at'])
            self.assertNotIn('synthetic-private', json.dumps(current))


if __name__ == '__main__':
    unittest.main()
