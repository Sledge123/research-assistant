"""文献下载提醒 SMTP 行为；只用假 SMTP，不连接邮件服务器。"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest

from highscore_downloader.mail_settings import MailSettings
from highscore_literature.mail_worker import ReminderMailer
from highscore_literature.worker import WorkerError


class FakeSMTP:
    def __init__(self, *, fail_after_submit=False):
        self.fail_after_submit=fail_after_submit
        self.sent=0

    def login(self,username,password):
        self.asserted=(username,password)

    def send_message(self,message,from_addr,to_addrs):
        self.sent+=1
        self.message=message
        self.asserted_send=(from_addr,to_addrs)
        if self.fail_after_submit:
            raise OSError('synthetic network interruption')
        return {}

    def quit(self):
        pass


class FakeRefusedSMTP(FakeSMTP):
    def send_message(self,message,from_addr,to_addrs):
        self.sent+=1
        return {to_addrs[0]:(550,'synthetic rejection')}


class LiteratureReminderMailerTests(unittest.TestCase):
    def setUp(self):
        cache=Path(__file__).resolve().parents[1]/'.cache/tests'
        cache.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='literature-mail-',dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.settings=MailSettings(username='sender@example.test',authorization_code='synthetic-smtp-auth',
                                   smtp_host='smtp.example.test',smtp_port=465,enabled=True)
        self.reminder={'id':'a'*32,'task_id':'b'*32,'title':'Synthetic paper title',
                       'recipient':'reader@example.test',
                       'download_expires_at':(datetime.now(timezone.utc)+timedelta(days=3)).timestamp(),
                       'attempt':1}

    def test_smtp_acceptance_is_recorded_and_duplicate_local_claim_does_not_resend(self):
        smtp=FakeSMTP()
        mailer=ReminderMailer(self.temp.name,self.settings,'https://example.com/',
                              smtp_factory=lambda *args,**kwargs:smtp)
        self.assertEqual(mailer.send(self.reminder),'submitted')
        self.assertEqual(mailer.send(self.reminder),'submitted')
        self.assertEqual(smtp.sent,1)
        self.assertIn('请登录科研助手',smtp.message.get_content())
        self.assertIn('https://example.com/',smtp.message.get_content())
        self.assertNotIn('synthetic-smtp-auth',smtp.message.as_string())

    def test_unknown_smtp_result_is_persisted_and_never_automatically_retried(self):
        smtp=FakeSMTP(fail_after_submit=True)
        mailer=ReminderMailer(self.temp.name,self.settings,'https://example.com/',
                              smtp_factory=lambda *args,**kwargs:smtp)
        self.assertEqual(mailer.send(self.reminder),'unknown')
        self.assertEqual(mailer.send(self.reminder),'unknown')
        self.assertEqual(smtp.sent,1)

    def test_definite_rejection_retries_only_on_a_new_server_attempt(self):
        smtp=FakeRefusedSMTP()
        mailer=ReminderMailer(self.temp.name,self.settings,'https://example.com/',
                              smtp_factory=lambda *args,**kwargs:smtp)
        self.assertEqual(mailer.send(self.reminder),'failed')
        self.assertEqual(mailer.send(self.reminder),'failed')
        self.assertEqual(smtp.sent,1)
        retry={**self.reminder,'attempt':2}
        self.assertEqual(mailer.send(retry),'failed')
        self.assertEqual(smtp.sent,2)

    def test_invalid_site_entry_is_rejected(self):
        with self.assertRaises(WorkerError):
            ReminderMailer(self.temp.name,self.settings,'http://example.com/')


if __name__=='__main__':
    unittest.main()
