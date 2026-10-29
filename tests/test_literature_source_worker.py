"""按任务取正文：历史失效记录、自动接续和未知提交恢复的真实引擎回归。"""
import base64
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from highscore_literature.browser import AblesciBrowser, Attachment, Listing, RequestRecord
from highscore_literature.engine import LiteratureEngine, StateStore
from test_literature_worker import sample_pdf, TITLE, ACCOUNT

ROOT = Path(__file__).resolve().parents[1]


class TaskSourceTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / '.cache/tests'
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='literature-source-', dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        network = patch.object(socket, 'create_connection', side_effect=AssertionError('离线禁止联网'))
        network.start()
        self.addCleanup(network.stop)
        self.browser = Mock()
        self.browser.__enter__ = Mock(return_value=self.browser)
        self.browser.__exit__ = Mock(return_value=False)
        self.browser.list_requests.return_value = Listing([RequestRecord('old', TITLE, '已完结')], True)
        self.browser.prepare_publish.return_value = {'ids': ['old'], 'signature': 'a' * 64}
        self.browser.recover_publish.return_value = None
        self.browser.source.return_value = SimpleNamespace(status='awaiting', attachment=None,
                                                         evidence_sha256='a' * 64)
        self.browser.attachment.return_value = None
        self.browser.publish.side_effect = self.publish
        self.factory = Mock(return_value=self.browser)
        self.engine = LiteratureEngine(self.root, accounts=[ACCOUNT], browser_factory=self.factory)

    def job(self, **values):
        return {'id': 'job-one', 'title': TITLE, 'canonical_title': TITLE, 'doi': None,
                'account_ref': None, 'external_request_id': None, 'source_revision': 0,
                'created_at': time.time() - 60, 'deadline': time.time() + 3600, **values}

    def publish(self, title, doi, before_submit):
        before_submit()
        return RequestRecord('new-' + str(self.browser.publish.call_count), title, '待应助')

    def test_old_finished_history_does_not_bind_or_block_fresh_article(self):
        self.engine.store.write(ACCOUNT['account_ref'], TITLE,
                                {'external_request_id': 'old', 'phase': 'waiting'})
        result = self.engine(self.job(), Mock())
        self.assertEqual(result.get('external_request_id'), 'new-1')
        self.browser.publish.assert_called_once()
        self.browser.list_requests.assert_not_called()
        self.assertEqual(result.get('stage'), 'awaiting_fulltext')
        legacy = self.engine.store.read(ACCOUNT['account_ref'], TITLE)
        self.assertEqual(legacy['external_request_id'], 'old')

    def test_bound_expired_file_requests_server_reset_without_new_publish(self):
        self.browser.source.return_value.status = 'expired'
        result = self.engine(self.job(external_request_id='old', account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(result.get('source_reset'), {'previous_request_id': 'old', 'source_revision': 0,
                         'reason': 'expired', 'evidence_sha256': 'a' * 64})
        self.assertEqual((result['state'], result.get('stage')), ('waiting', 'renewing'))
        self.browser.publish.assert_not_called()

    def test_supplement_only_is_replaced_even_when_attachment_is_present(self):
        self.browser.source.return_value = SimpleNamespace(status='supplement_only',
            attachment=Attachment('supplement', 'supplement.pdf'), evidence_sha256='b' * 64)
        result = self.engine(self.job(external_request_id='old', account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(result.get('source_reset', {}).get('reason'), 'supplement_only')
        self.browser.download.assert_not_called()
        self.browser.accept.assert_not_called()

    def test_newly_published_source_is_bound_before_requesting_reset(self):
        self.browser.source.return_value.status = 'supplement_only'
        first = self.engine(self.job(), Mock())
        self.assertEqual(first.get('external_request_id'), 'new-1')
        self.assertNotIn('source_reset', first, '服务器未绑定的编号不能直接重置')
        self.assertEqual(first.get('stage'), 'renewing')

    def test_new_revision_keeps_old_attempt_and_publishes_once(self):
        first = self.job(external_request_id='old', account_ref=ACCOUNT['account_ref'])
        self.browser.source.return_value.status = 'expired'
        self.engine(first, Mock())
        self.browser.source.return_value.status = 'awaiting'
        result = self.engine(self.job(source_revision=1, account_ref=ACCOUNT['account_ref']), Mock())
        again = self.engine(self.job(source_revision=1, account_ref=ACCOUNT['account_ref'],
                                     external_request_id=result.get('external_request_id')), Mock())
        self.assertEqual(result.get('external_request_id'), 'new-1')
        self.assertEqual(again.get('external_request_id'), 'new-1')
        self.assertEqual(self.browser.publish.call_count, 1)
        states = [json.loads(path.read_text(encoding='utf-8'))
                  for path in (self.root/'output/literature-worker/jobs').rglob('state.json')]
        self.assertEqual({row['source_revision'] for row in states}, {0, 1})

    def test_unknown_submit_is_scoped_to_current_job_and_not_replayed(self):
        def uncertain(title, doi, before_submit):
            before_submit()
            states = list((self.root/'output/literature-worker/jobs/job-one').rglob('state.json'))
            self.assertEqual(len(states), 1)
            state = json.loads(states[0].read_text(encoding='utf-8'))
            self.assertEqual(state['phase'], 'publish_intent')
            self.assertEqual(state['publish_baseline']['ids'], ['old'])
            raise OSError('synthetic-private-secret')
        self.browser.publish.side_effect = uncertain
        first = self.engine(self.job(), Mock())
        again = self.engine(self.job(), Mock())
        self.assertNotEqual(again['state'], 'succeeded')
        self.assertEqual(self.browser.publish.call_count, 1)
        self.browser.recover_publish.assert_called_once()
        self.browser.publish.side_effect = self.publish
        other = self.engine(self.job(id='job-two'), Mock())
        self.assertEqual(other.get('external_request_id'), 'new-2')
        self.assertNotIn('synthetic-private-secret', json.dumps([first, again, other]))

    def test_unknown_submit_pins_original_account_before_server_first_callback(self):
        second_account = {**ACCOUNT, 'account_ref': 'synthetic-b'}
        self.engine.accounts = [ACCOUNT, second_account]
        def uncertain(title, doi, before_submit):
            before_submit()
            raise OSError('本次提交后连接中断')
        self.browser.publish.side_effect = uncertain
        self.engine(self.job(), Mock())
        self.engine.accounts = [second_account, ACCOUNT]
        self.browser.publish.side_effect = self.publish
        resumed = self.engine(self.job(), Mock())
        self.assertEqual(self.browser.publish.call_count, 1, '换账号顺序不能重发本任务未知提交')
        self.assertEqual(resumed.get('account_ref'), ACCOUNT['account_ref'])

    def test_verified_fulltext_cache_is_reused_without_reopening_old_source(self):
        data = sample_pdf()
        self.browser.source.return_value = SimpleNamespace(status='available',
            attachment=Attachment('fulltext', 'fulltext.pdf', False), evidence_sha256='c' * 64)
        self.browser.download.return_value = data
        first = self.engine(self.job(), Mock())
        self.assertEqual(first['state'], 'succeeded')
        self.factory.reset_mock()
        other = self.engine(self.job(id='job-two'), Mock())
        self.assertEqual(other['state'], 'succeeded')
        self.assertEqual(base64.b64decode(other['pdf_base64']), data)
        self.factory.assert_not_called()

    def test_other_identity_and_corrupt_historical_state_do_not_block_new_task(self):
        old = self.engine.store.entry(ACCOUNT['account_ref'], TITLE)/'state.json'
        old.parent.mkdir(parents=True)
        old.write_text('{broken historical state', encoding='utf-8')
        result = self.engine(self.job(), Mock())
        self.assertEqual(result.get('external_request_id'), 'new-1')

    def test_different_doi_historical_binding_does_not_block_same_title(self):
        self.engine.store.write(ACCOUNT['account_ref'], TITLE,
            {'external_request_id': 'old', 'doi': '10.1234/other', 'phase': 'waiting'})
        result = self.engine(self.job(doi='10.1234/current'), Mock())
        self.assertEqual((result.get('external_request_id'), result.get('doi')), ('new-1', '10.1234/current'))
        self.assertEqual(self.engine.store.read(ACCOUNT['account_ref'], TITLE)['doi'], '10.1234/other')

    def test_same_task_doi_cannot_change_in_new_source_revision(self):
        self.engine(self.job(doi='10.1234/original'), Mock())
        self.browser.publish.reset_mock()
        result = self.engine(self.job(doi='10.1234/different', source_revision=1,
                                     account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.browser.publish.assert_not_called()

    def test_upgrade_preserves_legacy_unknown_submit_for_same_active_task(self):
        legacy_job = self.job()
        legacy_job.pop('source_revision')
        self.browser.list_requests.return_value = Listing([], True)
        def uncertain(title, doi, before_submit):
            before_submit()
            raise OSError('旧版提交后首回调丢失')
        self.browser.publish.side_effect = uncertain
        self.engine(legacy_job, Mock())
        self.browser.publish.side_effect = self.publish
        result = self.engine({**legacy_job, 'source_revision': 0}, Mock())
        self.assertEqual(self.browser.publish.call_count, 1, '旧版同一任务未知提交不得因升级重发')
        self.browser.recover_publish.assert_called_once()
        self.assertEqual(result['state'], 'needs_attention')

    def test_new_job_after_legacy_unknown_intent_is_not_blocked_by_history(self):
        legacy = self.engine.store.write(ACCOUNT['account_ref'], TITLE,
            {'phase': 'publish_intent', 'intent_at': time.time()-100})
        result = self.engine(self.job(created_at=time.time()-20), Mock())
        self.assertEqual(result.get('external_request_id'), 'new-1')
        self.browser.recover_publish.assert_not_called()

    def legacy_waiting_without_doi(self):
        selected = {'id': 'legacy-auto-choice', 'title': TITLE, 'doi': None,
                    'journal': 'Journal A', 'year': 2025}
        self.engine.resolver = Mock()
        self.engine.resolver.resolve.return_value = {'canonical_title': TITLE, 'doi': None,
                                                     'candidate': selected}
        self.browser.list_requests.return_value = Listing([], True)
        self.browser.verify_identity.return_value = True
        legacy_job = self.job(canonical_title=None)
        legacy_job.pop('source_revision')
        first = self.engine(legacy_job, Mock())
        self.assertEqual(first['state'], 'waiting')
        self.browser.bind_identity.reset_mock()
        self.browser.verify_identity.reset_mock()
        self.browser.source.return_value = SimpleNamespace(status='available',
            attachment=Attachment('fulltext', 'fulltext.pdf', False), evidence_sha256='c'*64)
        self.browser.verify_identity.return_value = True
        return selected, self.job(external_request_id=first['external_request_id'],
                                  account_ref=ACCOUNT['account_ref'])

    def test_upgrade_bound_no_doi_keeps_original_journal_and_rejects_wrong_pdf(self):
        selected, job = self.legacy_waiting_without_doi()
        self.browser.download.return_value = sample_pdf(header='Journal B 2026')
        result = self.engine(job, Mock())
        self.assertNotEqual(result['state'], 'succeeded', '升级不得丢失旧任务的期刊年份身份')
        self.browser.bind_identity.assert_called_once_with(selected)
        self.browser.accept.assert_not_called()

    def test_upgrade_bound_no_doi_accepts_matching_fulltext_without_new_publish(self):
        selected, job = self.legacy_waiting_without_doi()
        self.browser.download.return_value = sample_pdf(header='Journal A 2025')
        result = self.engine(job, Mock())
        self.assertEqual(result['state'], 'succeeded')
        self.browser.bind_identity.assert_called_once_with(selected)
        self.browser.verify_identity.assert_called_once()
        self.assertEqual(self.browser.publish.call_count, 1)

    def test_no_doi_accept_failure_resumes_existing_verified_pdf(self):
        selected = {'id': 'synthetic-choice', 'title': TITLE, 'doi': None,
                    'journal': 'Example Journal', 'year': 2026}
        self.browser.source.return_value = SimpleNamespace(status='available',
            attachment=Attachment('fulltext', 'fulltext.pdf', True), evidence_sha256='c'*64)
        self.browser.verify_identity.return_value = True
        self.browser.download.return_value = sample_pdf(header='Example Journal 2026')
        self.browser.accept.side_effect = OSError('采纳确认连接中断')
        first = self.engine(self.job(selected_candidate=selected), Mock())
        self.assertNotEqual(first['state'], 'succeeded')
        self.browser.accept.side_effect = None
        resumed = self.engine(self.job(external_request_id=first.get('external_request_id'),
                                      account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(resumed['state'], 'succeeded')
        self.assertEqual(self.browser.download.call_count, 1)
        self.assertEqual(self.browser.accept.call_count, 2)

    def test_no_doi_selected_identity_survives_wait_and_source_revision(self):
        selected = {'id': 'synthetic-choice', 'title': TITLE, 'doi': None,
                    'journal': 'Example Journal', 'year': 2026}
        self.engine.resolver = Mock()
        self.engine.resolver.resolve.return_value = {'canonical_title': TITLE, 'doi': None, 'candidate': selected}
        first = self.engine(self.job(canonical_title=None), Mock())
        self.assertEqual(first['state'], 'waiting')
        self.engine(self.job(external_request_id=first.get('external_request_id'),
                             account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(self.browser.bind_identity.call_count, 2, '重启后须恢复期刊/年份身份')
        self.engine(self.job(source_revision=1, account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(self.browser.bind_identity.call_args.args[0], selected)

    def test_bad_historical_retention_field_does_not_stop_cleanup_or_new_job(self):
        self.engine.store.write(ACCOUNT['account_ref'], TITLE,
            {'phase': 'waiting', 'pdf_at': 'invalid historical timestamp'})
        self.engine.store.cleanup()
        result = self.engine(self.job(), Mock())
        self.assertEqual(result.get('external_request_id'), 'new-1')


class SourcePageTests(unittest.TestCase):
    def browser(self, body, badge='已完结', attachment=None):
        browser = object.__new__(AblesciBrowser)
        browser.page = Mock()
        browser.guard = Mock()
        browser._goto = Mock()
        browser._clear_notification = Mock()
        browser._verify_detail = Mock()
        browser._timeout_ms = Mock(return_value=1000)
        browser.page.locator.return_value.count.return_value = 1
        browser.page.locator.return_value.inner_text.return_value = body
        browser.page.locator.return_value.first.inner_text.return_value = badge
        browser.attachment = Mock(return_value=attachment)
        return browser

    def test_real_expired_supplement_marker_is_not_waiting(self):
        body = ('已完结\n标题\n[求助补充材料] ' + TITLE + '\n网址\nhttps://doi.org/10.1234/test\n'
                '下载\n该求助完结已超 24 小时，文件已从服务器自动删除，无法下载。')
        browser = self.browser(body)
        self.assertTrue(callable(getattr(type(browser), 'source', None)), '缺少正文来源状态识别')
        result = browser.source('old', TITLE, None)
        self.assertEqual(result.status, 'supplement_only')
        self.assertEqual(result.evidence_sha256, hashlib.sha256(body.encode('utf-8')).hexdigest())
        browser.attachment.assert_not_called()

    def test_expired_article_is_expired_and_closed_without_file_is_terminal(self):
        for body, expected in [
            ('标题\n' + TITLE + '\n网址\n地址\n文件已从服务器自动删除，无法下载。', 'expired'),
            ('标题\n' + TITLE + '\n网址\n地址\n该求助已完结，感谢关注', 'finished_without_file')]:
            with self.subTest(expected=expected):
                browser = self.browser(body)
                self.assertTrue(callable(getattr(type(browser), 'source', None)), '缺少正文来源状态识别')
                self.assertEqual(browser.source('old', TITLE, None).status, expected)

    def test_unknown_page_state_cannot_claim_awaiting_fulltext(self):
        from highscore_literature.engine import LiteratureError
        browser = self.browser('标题\n' + TITLE + '\n网址\n地址', badge='未知状态')
        with self.assertRaises(LiteratureError):
            browser.source('old', TITLE, None)


class SourceProgressTests(unittest.TestCase):
    def test_child_progress_requires_parent_acknowledgement(self):
        from highscore_literature.process_runner import _RemoteGuard
        channel = Mock()
        channel.poll.return_value = True
        channel.recv_bytes.return_value = b'ok'
        guard = _RemoteGuard(channel, time.monotonic() + 10)
        self.assertTrue(callable(getattr(type(guard), 'set_stage', None)), '子进程尚不能回传业务阶段')
        guard.set_stage('verifying')
        channel.send_bytes.assert_called_once_with(b'stage:verifying')

    def test_parent_progress_uses_lease_guarded_heartbeat(self):
        from highscore_literature.worker import LeaseGuard
        client = Mock()
        client.heartbeat.return_value = {'cancel_requested': False}
        guard = LeaseGuard(client, {'id': 'job-one', 'deadline': time.time()+3600}, 'synthetic-lease')
        self.assertTrue(callable(getattr(type(guard), 'set_stage', None)), '父进程尚不能提交业务阶段')
        guard.set_stage('verifying')
        client.heartbeat.assert_called_once_with('job-one', 'synthetic-lease', stage='verifying')


class PublishRecoveryTests(unittest.TestCase):
    def test_old_same_title_wrong_doi_does_not_hide_unique_current_submit(self):
        from datetime import datetime, timedelta, timezone
        from highscore_literature.browser import ORIGIN
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        bodies = {
            'old': '标题\n'+TITLE+'\n网址\nhttps://doi.org/10.1234/other\nDOI\n10.1234/other\n求助人 测试 在 2020-01-01 00:00:00 发布',
            'current': '标题\n'+TITLE+'\n网址\nhttps://doi.org/10.1234/current\nDOI\n10.1234/current\n求助人 测试 在 '+stamp+' 发布',
        }
        browser = object.__new__(AblesciBrowser)
        browser.identity = None
        browser.store = Mock()
        browser.store.now.return_value = now+1
        browser.page = Mock()
        browser._timeout_ms = Mock(return_value=1000)
        browser._clear_notification = Mock()
        browser._head = Mock(return_value=([RequestRecord('old', TITLE), RequestRecord('current', TITLE)], {}, 'a'*64))
        def goto(url, path):
            browser.page.url = url
            identifier = url.rsplit('=', 1)[1]
            browser.page.locator.return_value.inner_text.return_value = bodies[identifier]
        browser._goto = Mock(side_effect=goto)
        result = browser.recover_publish({'ids': ['baseline'], 'signature': 'a'*64}, TITLE, '10.1234/current', now)
        self.assertEqual(result.id, 'current')


if __name__ == '__main__':
    unittest.main()
