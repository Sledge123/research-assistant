"""执行器安全回归：使用 synthetic 数据，禁止联网、RPA 与真实邮件。"""
import base64
from contextlib import nullcontext
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from test_archive import synthetic_card

ROOT = Path(__file__).resolve().parents[1]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / '.cache/tests'
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='worker-', dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('离线禁止联网'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def config(self, **values):
        from highscore_worker.config import WorkerConfig
        return WorkerConfig(self.root, 'https://example.com/highscore/api', 'https://example.com/highscore/',
                            'synthetic-worker', 'synthetic-db', 'synthetic-token', **values)

    def job(self, **values):
        return {'id': 'synthetic-job', 'kind': 'archive', 'user_id': 'user-a',
                'database_id': 'synthetic-db', 'email': 'user@example.com',
                'payload': {'codes': '09-999-0001', 'send_mail': False}, **values}

    def test_configuration_is_https_and_project_relative(self):
        from highscore_worker.config import load_config, WorkerError
        local = self.root / 'worker.local.json'
        local.write_text(json.dumps({'server_url': 'http://example.com/highscore/api',
                                    'public_url': 'https://example.com/highscore/',
                                    'worker_id': 'worker', 'database_id': 'db'}))
        with self.assertRaises(WorkerError):
            load_config(self.root, 'worker.local.json', environ={'HIGHSCORE_WORKER_TOKEN': 'secret'})
        with self.assertRaises(WorkerError):
            load_config(self.root, '../outside.json', environ={})

    def test_verification_url_rejects_external_host_duplicate_token_and_extra_fields(self):
        from highscore_worker.verification import validate_verification_payload
        from highscore_worker.config import WorkerError
        valid = {'to': 'user@example.com', 'verify_url': 'https://example.com/highscore/?verify=synthetic_token'}
        self.assertEqual(validate_verification_payload(valid, self.config().public_url)[0], valid['to'])
        for url in ('https://evil.example/highscore/?verify=x',
                    'https://example.com/other/?verify=x',
                    'https://example.com/highscore/?verify=x&verify=y',
                    'https://example.com/highscore/?verify=x&redirect=elsewhere',
                    'https://example.com@evil.example/highscore/?verify=x'):
            with self.subTest(url=url), self.assertRaises(WorkerError):
                validate_verification_payload({**valid, 'verify_url': url}, self.config().public_url)
        with self.assertRaises(WorkerError):
            validate_verification_payload({**valid, 'body': '任意正文'}, self.config().public_url)

    def test_user_and_database_roots_are_isolated(self):
        from highscore_database.executor import isolation_root
        roots = [isolation_root(self.root, user, db) for user, db in
                 [('user-a', 'db-a'), ('user-b', 'db-a'), ('user-a', 'db-b'), ('../escape', '../escape')]]
        self.assertEqual(len(set(roots)), 4)
        self.assertTrue(all(root.is_relative_to(self.root / 'output/worker/users') for root in roots))

    def test_upload_zip_contains_only_revalidated_archive_files(self):
        from highscore_downloader.archive import ArchiveStore
        from highscore_database.executor import package_archive
        native = self.root / 'synthetic.rtf'
        native.write_bytes(br'{\rtf1 synthetic}')
        receipt = ArchiveStore(self.root).save(synthetic_card(), native, '09-999-0001')
        result = {'status': 'success', 'receipt_path': receipt['receipt_path']}
        encoded = package_archive(self.root, [result])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded))) as archive:
            self.assertEqual(len(archive.namelist()), 5)
            self.assertTrue(all(name.startswith('output/cards/09-999-0001/') for name in archive.namelist()))
        (self.root / receipt['files']['txt']['path']).write_text('破坏', encoding='utf-8')
        with self.assertRaises(Exception):
            package_archive(self.root, [result])

    def test_unverified_candidate_source_is_never_accepted(self):
        from highscore_database.executor import JobHandler
        from highscore_database.reader import DatabaseError
        from types import SimpleNamespace
        reader = SimpleNamespace(database_id='synthetic-db',source='Synthetic 数据库')
        handler = JobHandler(self.config())
        job=self.job(payload={'codes':'09-999-0001','selected':[{'code':'09-999-0001','source':'其他来源'}]})
        with self.assertRaisesRegex(DatabaseError,'来源'):
            handler.archive(job,reader,self.root,threading.Event(),lambda _:None)

    def test_lost_lease_stops_running_job_and_never_reports_stale_success(self):
        from highscore_database.executor import Worker
        client = Mock()
        client.claim.return_value = {'job': self.job(), 'lease_token': 'synthetic-lease', 'lease_seconds': .04}
        client.heartbeat.side_effect = [{'cancel_requested':False}, OSError('断线')]
        stopped = threading.Event()
        def run(job, stop, progress):
            self.assertTrue(stop.wait(1))
            stopped.set()
            return {'state': 'succeeded', 'result': {}}
        worker = Worker(self.config(), client=client, handler=run, mutex_factory=nullcontext)
        self.assertFalse(worker.run_once())
        self.assertTrue(stopped.is_set())
        client.complete.assert_not_called()

    def test_database_mismatch_is_rejected_before_rpa(self):
        from highscore_database.executor import JobHandler
        handler = JobHandler(self.config(), runner_factory=Mock(side_effect=AssertionError('禁止RPA')))
        result = handler(self.job(database_id='other-db'), threading.Event(), lambda _: None)
        self.assertEqual(result['state'], 'needs_attention')

    def test_verification_unknown_is_persisted_and_not_automatically_replayed(self):
        from highscore_downloader.mail_settings import MailSettings
        from highscore_worker.verification import VerificationMailer
        smtp = Mock()
        smtp.send_message.side_effect = TimeoutError('提交后断线')
        factory = Mock(return_value=smtp)
        sender = VerificationMailer(self.root, MailSettings(username='sender@example.com', authorization_code='synthetic'),
                                    'https://example.com/highscore/', smtp_factory=factory)
        payload = {'to': 'user@example.com', 'verify_url': 'https://example.com/highscore/?verify=synthetic'}
        self.assertEqual(sender.send('synthetic-job', payload, threading.Event())['status'], 'unknown')
        self.assertEqual(sender.send('synthetic-job', payload, threading.Event())['status'], 'unknown')
        self.assertEqual(factory.call_count, 1)
        self.assertFalse(any('synthetic' in p.read_text(encoding='utf-8') for p in
                             (self.root / 'output/verification-mail').glob('*.json')))

    def test_main_without_run_never_claims_tasks(self):
        from highscore_database.__main__ import main
        self.assertEqual(main([]), 0)


if __name__ == '__main__':
    unittest.main()
