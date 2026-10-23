"""提交前再次核验租约和取消状态，不能只依赖轮询心跳。"""
import inspect
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from highscore_downloader.archive import ArchiveStore
from highscore_downloader.mailer import MailService
from highscore_downloader.mail_settings import MailSettings
from test_archive import synthetic_card
from test_mail import FakeSMTP

ROOT = Path(__file__).resolve().parents[1]


class SubmissionGuardTests(unittest.TestCase):
    def test_cancel_during_pre_submit_guard_never_sends(self):
        self.assertIn('submission_guard', inspect.signature(MailService).parameters, '尚未接入提交前状态核验')
        base = ROOT / '.cache/tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary, patch.object(socket, 'create_connection', side_effect=AssertionError('禁止联网')):
            root = Path(temporary)
            rtf = root / 'synthetic.rtf'
            rtf.write_bytes(br'{\rtf1 synthetic}')
            receipt = ArchiveStore(root).save(synthetic_card(), rtf, '09-999-0001')
            stop = threading.Event()
            smtp = FakeSMTP()
            calls = []
            def guard():
                calls.append('guard')
                stop.set()
            service = MailService(root, MailSettings(username='test@example.com', authorization_code='synthetic'),
                                  smtp_factory=lambda *a, **k: smtp, stop_event=stop, submission_guard=guard)
            result = service.deliver([{'code': receipt['code'], 'status':'success','receipt_path':receipt['receipt_path']}], 'user@example.com')
            self.assertEqual(calls, ['guard'])
            self.assertEqual(smtp.calls, ['login'])
            self.assertEqual(result['status'], 'stopped')
