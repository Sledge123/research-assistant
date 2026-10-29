"""Linux 执行器离线集成：真实打包及复验，SMTP/网络均为 synthetic。"""
import base64
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from test_database_reader import fixture
from highscore_database.reader import DatabaseReader
from highscore_database.executor import JobHandler,isolation_root,report_root,Worker
from highscore_worker.config import WorkerConfig
from highscore_downloader.mail_settings import MailSettings
from highscore_downloader.mailer import MailService
from highscore_web.artifacts import accept_zip,checked_zip

ROOT=Path(__file__).resolve().parents[1]


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='db-executor-',dir=base);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.database=self.root/'synthetic.hsrdb';fixture(self.database)
        self.reader=DatabaseReader(self.database)
        self.config=WorkerConfig(self.root,'https://example.com/highscore/api','https://example.com/highscore/',
                                 'synthetic-worker',self.reader.database_id,'synthetic-token')
        self.settings=MailSettings(username='sender@example.com',authorization_code='synthetic-only')
        self.smtp=Mock();self.smtp.send_message.return_value={};self.smtp_factory=Mock(return_value=self.smtp)
        self.handler=JobHandler(self.config,reader_factory=lambda stop:self.reader,
             mail_settings_loader=lambda root:self.settings,
             mail_factory=lambda root,settings,stop_event:MailService(root,settings,stop_event=stop_event,smtp_factory=self.smtp_factory))
        self.network=patch.object(socket,'create_connection',side_effect=AssertionError('禁止联网'))
        self.network.start();self.addCleanup(self.network.stop)

    def job(self,**values):
        return {'id':'synthetic-archive','kind':'archive','user_id':'synthetic-user','database_id':self.reader.database_id,
                'email':'receiver@example.com','payload':{'codes':'09-999-0001','send_mail':False},**values}

    def run_job(self,job):return self.handler(job,threading.Event(),lambda _:None)

    def test_worker_reuses_verified_reader_and_checks_signature_on_every_query(self):
        handler=JobHandler(self.config)
        job=self.job(kind='search',payload={'mode':'formula','query':'X Y'})
        with patch('highscore_database.executor.load_reader',return_value=self.reader) as factory:
            for _ in range(2):
                self.assertEqual(handler(job,threading.Event(),lambda _:None)['state'],'succeeded')
            self.assertEqual(factory.call_count,1)
            self.database.touch()
            self.assertEqual(handler(job,threading.Event(),lambda _:None)['state'],'needs_attention')

    def test_search_to_selected_archive_upload_and_download_revalidation(self):
        search=self.run_job(self.job(kind='search',payload={'mode':'formula','query':'X Y'}))
        self.assertEqual(search['state'],'succeeded');candidate=search['result']['candidates'][0]
        output=self.run_job(self.job(payload={'codes':candidate['code'],'selected':[candidate],'send_mail':False}))
        self.assertEqual(output['result']['archive_status'],'success')
        relative,receipts=accept_zip(self.root,'synthetic-upload',output['artifact_zip_base64'],[candidate])
        self.assertEqual(receipts[0]['export_method'],'sqlite_readonly_database_report')
        self.assertTrue(checked_zip(self.root,relative,[candidate]))
        self.smtp_factory.assert_not_called()

    def test_successful_mail_deduplicates_but_explicit_retry_creates_new_delivery(self):
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        first=self.run_job(job)
        self.assertEqual(first['result']['mail_status'],'submitted')
        duplicate=self.run_job({**job,'id':'synthetic-second'})
        self.assertEqual(duplicate['result']['mail_status'],'submitted')
        self.assertEqual(self.smtp.send_message.call_count,1)
        retry=self.run_job(self.job(id='synthetic-retry',kind='mail_retry',payload={
            'original_job_id':job['id'],'codes':'09-999-0001','force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'submitted')
        self.assertEqual(self.smtp.send_message.call_count,2)
        root=report_root(isolation_root(self.root,job['user_id'],self.reader.database_id))
        self.assertEqual(len(list((root/'output/mail').glob('*/job.json'))),2)

    def test_report_upgrade_keeps_legacy_mail_and_resolves_its_explicit_retry(self):
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        base=isolation_root(self.root,job['user_id'],self.reader.database_id)
        self.handler.archive(job,self.reader,base,threading.Event(),lambda _:None)
        original=list((base/'output/mail').glob('*/job.json'))[0].read_bytes()
        output=self.run_job(self.job(id='synthetic-report-v2'))
        self.assertEqual(output['state'],'succeeded')
        self.assertEqual(list((base/'output/mail').glob('*/job.json'))[0].read_bytes(),original)
        retry=self.run_job(self.job(kind='mail_retry',payload={
            'original_job_id':job['id'],'codes':'09-999-0001','force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'submitted')
        self.assertEqual(self.smtp.send_message.call_count,2)

    def test_report_upgrade_does_not_bypass_uncertain_legacy_delivery(self):
        self.smtp.send_message.side_effect=TimeoutError('synthetic')
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        base=isolation_root(self.root,job['user_id'],self.reader.database_id)
        self.assertEqual(self.handler.archive(job,self.reader,base,threading.Event(),lambda _:None)
                         ['result']['mail_status'],'unknown')
        output=self.run_job({**job,'id':'synthetic-report-v2'})
        self.assertEqual(output['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)
        retry=self.run_job(self.job(kind='mail_retry',payload={
            'original_job_id':'synthetic-report-v2','codes':'09-999-0001',
            'force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_unknown_version_two_mail_also_blocks_version_three(self):
        self.smtp.send_message.side_effect=TimeoutError('synthetic')
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        base=isolation_root(self.root,job['user_id'],self.reader.database_id)
        previous=base/'report-v2';previous.mkdir()
        self.handler.archive(job,self.reader,previous,threading.Event(),lambda _:None)
        output=self.run_job({**job,'id':'synthetic-v3'})
        self.assertEqual(output['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)
        retry=self.run_job(self.job(kind='mail_retry',payload={
            'original_job_id':'synthetic-v3','codes':'09-999-0001',
            'force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_unknown_version_three_mail_also_blocks_current_report(self):
        self.smtp.send_message.side_effect=TimeoutError('synthetic')
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        base=isolation_root(self.root,job['user_id'],self.reader.database_id)
        previous=base/'report-v3';previous.mkdir()
        self.handler.archive(job,self.reader,previous,threading.Event(),lambda _:None)
        output=self.run_job({**job,'id':'synthetic-v4'})
        self.assertEqual(output['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)
        retry=self.run_job(self.job(kind='mail_retry',payload={
            'original_job_id':'synthetic-v4','codes':'09-999-0001',
            'force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_audited_report_refresh_uses_new_files_for_explicit_mail_retry(self):
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        base=isolation_root(self.root,job['user_id'],self.reader.database_id)
        self.handler.archive(job,self.reader,base,threading.Event(),lambda _:None)
        old_mapping=self.handler._mapping(base,job['id']).read_bytes()
        current=report_root(base)
        self.handler.archive({**job,'payload':{'codes':'09-999-0001','send_mail':False}},
                             self.reader,current,threading.Event(),lambda _:None)
        path=self.handler._mapping(current,job['id'])
        mapping=json.loads(path.read_text(encoding='utf-8'))
        mapping['supersedes_mapping_sha256']=hashlib.sha256(old_mapping).hexdigest()
        path.write_text(json.dumps(mapping),encoding='utf-8')
        retry=self.run_job(self.job(kind='mail_retry',payload={
            'original_job_id':job['id'],'codes':'09-999-0001','force_new_delivery':True}))
        self.assertEqual(retry['result']['mail_status'],'submitted')
        self.assertEqual(self.smtp.send_message.call_count,2)
        self.assertEqual(self.handler._mapping(base,job['id']).read_bytes(),old_mapping)
        self.assertEqual(len(list((current/'output/mail').glob('*/job.json'))),1)

    def test_unknown_delivery_blocks_automatic_resend_and_requires_confirmation(self):
        self.smtp.send_message.side_effect=TimeoutError('synthetic')
        job=self.job(payload={'codes':'09-999-0001','send_mail':True})
        first=self.run_job(job);self.assertEqual(first['result']['mail_status'],'unknown')
        duplicate=self.run_job({**job,'id':'synthetic-second'})
        self.assertEqual(duplicate['result']['mail_status'],'unknown')
        payload={'original_job_id':job['id'],'codes':'09-999-0001','force_new_delivery':True,'confirm_unknown':False}
        self.assertEqual(self.run_job(self.job(kind='mail_retry',payload=payload))['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)
        self.run_job(self.job(kind='mail_retry',payload={**payload,'confirm_unknown':True}))
        self.assertEqual(self.smtp.send_message.call_count,2)

    def test_different_user_cannot_reuse_original_mail_mapping(self):
        job=self.job(payload={'codes':'09-999-0001','send_mail':True});self.run_job(job)
        output=self.run_job(self.job(kind='mail_retry',user_id='other-user',payload={
            'original_job_id':job['id'],'codes':'09-999-0001','force_new_delivery':True,'confirm_unknown':True}))
        self.assertEqual(output['state'],'needs_attention')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_first_explicit_mail_after_download_only_archive_creates_delivery(self):
        job=self.job();output=self.run_job(job)
        self.assertEqual(output['result']['mail_status'],'not_requested')
        self.smtp.send_message.assert_not_called()
        retry=self.run_job(self.job(kind='mail_retry',payload={'original_job_id':job['id'],
                    'codes':'09-999-0001','force_new_delivery':True,'confirm_unknown':False}))
        self.assertEqual(retry['result']['mail_status'],'submitted')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_partial_archive_attaches_only_revalidated_cards_and_remains_partial(self):
        output=self.run_job(self.job(payload={'codes':'09-999-0001,09-999-0099','send_mail':True}))
        self.assertEqual(output['result']['archive_status'],'partial')
        self.assertEqual(output['result']['mail_status'],'submitted')
        self.assertEqual(output['state'],'needs_attention')
        self.assertIn('artifact_zip_base64',output)
        self.assertEqual(self.smtp_factory.call_count,1)
        import io,zipfile
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(output['artifact_zip_base64']))) as archive:
            self.assertEqual(len(archive.namelist()),5)
            self.assertTrue(all('09-999-0001/' in name for name in archive.namelist()))

    def test_mail_only_claim_configuration_does_not_advertise_database_jobs(self):
        from dataclasses import replace
        config=replace(self.config,mail_only=True)
        self.assertEqual(config.capabilities,['verification_mail'])
        from highscore_database.__main__ import main
        with patch('highscore_database.__main__.ROOT',self.root),patch('highscore_database.__main__.load_reader',
                  side_effect=AssertionError('mail-only禁止打开数据库')):
            local={'server_url':config.server_url,'public_url':config.public_url,'worker_id':config.worker_id,
                   'database_id':'mail-only','token':'synthetic-token'}
            (self.root/'worker.local.json').write_text(json.dumps(local))
            self.assertEqual(main(['--mail-only','--check-config']),0)

    def test_cancelled_claim_never_runs_handler_or_smtp(self):
        client=Mock();client.claim.return_value={'job':self.job(payload={'codes':'09-999-0001','send_mail':True}),
                                                'lease_token':'synthetic-lease','lease_seconds':60}
        client.heartbeat.return_value={'cancel_requested':True}
        handler=Mock(side_effect=AssertionError('取消后禁止执行'))
        self.assertTrue(Worker(self.config,client=client,handler=handler).run_once())
        handler.assert_not_called();self.smtp.send_message.assert_not_called()
        self.assertEqual(client.complete.call_args.args[1]['state'],'cancelled')

    def test_cancellation_after_login_is_checked_before_archive_mail_submission(self):
        client=Mock();client.claim.return_value={'job':self.job(payload={'codes':'09-999-0001','send_mail':True}),
                                                'lease_token':'synthetic-lease','lease_seconds':60}
        events=[]
        def heartbeat(*args):
            events.append('heartbeat')
            return {'cancel_requested':len(events)>1}
        client.heartbeat.side_effect=heartbeat
        self.assertTrue(Worker(self.config,client=client,handler=self.handler).run_once())
        self.smtp.login.assert_called_once();self.smtp.send_message.assert_not_called()
        completion=client.complete.call_args.args[1]
        self.assertEqual(completion['state'],'cancelled')
        self.assertEqual(completion['result']['mail_status'],'not_started')

    def test_cancellation_before_verification_mail_submission_does_not_open_database(self):
        from highscore_worker.verification import VerificationMailer
        factory=lambda root,settings,public_url,submission_guard:VerificationMailer(root,settings,public_url,
                         submission_guard=submission_guard,smtp_factory=self.smtp_factory)
        handler=JobHandler(self.config,reader_factory=Mock(side_effect=AssertionError('验证邮件禁止打开数据库')),
                           mail_settings_loader=lambda root:self.settings,verification_factory=factory)
        client=Mock();client.claim.return_value={'job':self.job(kind='verification_mail',database_id=None,
                           payload={'to':'receiver@example.com','verify_url':'https://example.com/highscore/?verify=synthetic'}),
                           'lease_token':'synthetic-lease','lease_seconds':60}
        client.heartbeat.side_effect=[{'cancel_requested':False},{'cancel_requested':True},{'cancel_requested':True}]
        Worker(self.config,client=client,handler=handler).run_once()
        self.smtp.send_message.assert_not_called()
        self.assertEqual(client.complete.call_args.args[1]['result']['mail_status'],'not_started')


if __name__=='__main__':unittest.main()
