"""邮件协议离线回归：所有输入为 synthetic，禁止建立外网连接。"""
import copy
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import smtplib
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from test_archive import synthetic_card
from highscore_downloader.archive import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]


class FakeSMTP:
    def __init__(self, failure=None, quit_failure=False):
        self.failure = failure
        self.quit_failure = quit_failure
        self.calls = []

    def login(self, user, code):
        self.calls.append("login")
        if self.failure == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"synthetic private detail")

    def send_message(self, message, **kwargs):
        self.calls.append("send")
        self.message = message
        if self.failure == "timeout":
            raise TimeoutError("synthetic private detail")
        if self.failure == "reject":
            raise smtplib.SMTPDataError(554, b"synthetic private detail")
        return {}

    def quit(self):
        if self.quit_failure:
            raise OSError("synthetic private detail")

    def close(self):
        pass


class MailTests(unittest.TestCase):
    def setUp(self):
        from highscore_downloader.mailer import MailService
        from highscore_downloader.mail_settings import MailSettings
        base = ROOT / ".cache/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="mail-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArchiveStore(self.root)
        native = self.root / "synthetic.rtf"
        native.write_bytes(br"{\rtf1 synthetic}")
        self.receipt = self.store.save(synthetic_card(), native, "09-999-0001")
        self.result = {"status": "success", "code": "999-1", "actual_code": self.receipt["code"],
                       "receipt_path": self.receipt["receipt_path"], "message": "已完成"}
        self.settings = MailSettings(username="Synthetic@EXAMPLE.com", authorization_code="synthetic-test-only")
        self.fake = FakeSMTP()
        self.factory_calls = []

        def factory(*args, **kwargs):
            self.factory_calls.append((args, kwargs))
            return self.fake
        self.service = MailService(self.root, self.settings, smtp_factory=factory)
        self.guard = patch.object(socket, "create_connection", side_effect=AssertionError("离线测试禁止外网"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def prepare(self, results=None, recipient="Receiver@EXAMPLE.COM"):
        return self.service.prepare([self.result] if results is None else results, recipient)

    def second_result(self):
        card = synthetic_card()
        card["code"] = "09-999-0002"
        receipt = self.store.save(card, self.root / "synthetic.rtf", card["code"])
        return {"code": card["code"], "actual_code": card["code"], "receipt_path": receipt["receipt_path"],
                "status": "success", "message": "第二张 synthetic 归档完成"}

    def test_dry_run_reads_back_eml_and_zip_without_network_or_sent_record(self):
        result = self.service.deliver([self.result, {"status": "not_found", "code": "1-1", "message": "未找到"}],
                                      "Receiver@EXAMPLE.COM", dry_run=True)
        self.assertEqual(result["status"], "prepared")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["recipient"], "Receiver@example.com")
        self.assertEqual(self.factory_calls, [])
        job = json.loads((self.root / result["job_path"]).read_text(encoding="utf-8"))
        self.assertEqual(job["status"], "prepared")
        eml = BytesParser(policy=policy.default).parsebytes((self.root / job["message_path"]).read_bytes())
        body = eml.get_body(preferencelist=("plain",)).get_content()
        for text in (self.receipt["code"], "合成测试相", "X2 Y", "峰数：2", "未找到"):
            self.assertIn(text, body)
        attachments = list(eml.iter_attachments())
        self.assertEqual(len(attachments), 1)
        with zipfile.ZipFile(io.BytesIO(attachments[0].get_payload(decode=True))) as archive:
            self.assertEqual(set(archive.namelist()),
                             {f"09-999-0001/09-999-0001.{ext}" for ext in ("rtf", "txt", "json", "csv")} |
                             {"09-999-0001/receipt.json"})
        self.assertNotIn(self.settings.authorization_code, (self.root / job["message_path"]).read_text(encoding="utf-8"))
        self.assertNotIn(self.settings.authorization_code, (self.root / job["job_path"]).read_text(encoding="utf-8"))

    def test_sends_once_then_deduplicates_skipped_archive_and_same_batch(self):
        prepared = self.prepare([self.result, {**self.result, "status": "skipped"}])
        self.assertEqual(len(prepared["cards"]), 1)
        sent = self.service.send(prepared["job_id"])
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(self.fake.calls.count("send"), 1)
        self.assertEqual(self.service.send(prepared["job_id"])["status"], "sent")
        self.assertEqual(self.prepare()["status"], "skipped")
        from highscore_downloader.mailer import MailService
        restarted = MailService(self.root, self.settings, smtp_factory=lambda *a, **k: self.fail("不得重复连接"))
        self.assertEqual(restarted.deliver([self.result], "Receiver@example.com")["status"], "skipped")
        self.assertEqual(self.prepare(recipient="Other@example.com")["status"], "prepared")
        self.assertEqual(self.factory_calls[0][1]["timeout"], 30)
        self.assertTrue(self.factory_calls[0][1]["context"].check_hostname)

    def test_archive_change_can_be_sent_as_new_content(self):
        first = self.prepare()
        self.service.send(first["job_id"])
        card_path = self.root / self.receipt["files"]["json"]["path"]
        card = json.loads(card_path.read_bytes())
        card["chemical_formula"] = "X3 Y"
        payload = json.dumps(card, ensure_ascii=False).encode("utf-8")
        card_path.write_bytes(payload)
        import hashlib
        self.receipt["files"]["json"].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (self.root / self.receipt["receipt_path"]).write_text(json.dumps(self.receipt), encoding="utf-8")
        self.assertEqual(self.prepare()["status"], "prepared")

    def test_corrupt_and_failed_items_never_attached(self):
        self.assertEqual(self.prepare([{**self.result, "status": "failed"}])["status"], "skipped")
        (self.root / self.receipt["files"]["txt"]["path"]).write_text("损坏", encoding="utf-8")
        self.assertEqual(self.prepare()["status"], "skipped")
        self.assertEqual(self.factory_calls, [])

    def test_artifact_or_source_tampering_fails_before_connect(self):
        for kind in ("message_path", "zip_path", "source"):
            with self.subTest(kind=kind):
                prepared = self.service.deliver([self.result], "Receiver@example.com", dry_run=True)
                target = self.root / (self.receipt["files"]["rtf"]["path"] if kind == "source" else prepared[kind])
                original = target.read_bytes()
                target.write_bytes(original + b"changed")
                sent = self.service.send(prepared["job_id"])
                self.assertEqual(sent["status"], "failed")
                self.assertEqual(self.factory_calls, [])
                target.write_bytes(original)

    def test_unknown_preserves_message_id_and_needs_explicit_force(self):
        self.fake.failure = "timeout"
        job = self.prepare()
        first = self.service.send(job["job_id"])
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["message_id"], job["message_id"])
        self.assertNotIn("synthetic private", first["message"])
        self.assertEqual(self.service.send(job["job_id"])["status"], "unknown")
        self.assertEqual(self.prepare()["status"], "skipped")
        self.assertEqual(self.fake.calls.count("send"), 1)
        self.fake.failure = None
        forced = self.service.send(job["job_id"], force=True)
        self.assertEqual(forced["status"], "sent")
        self.assertNotEqual(forced["job_id"], job["job_id"])
        self.assertNotEqual(forced["message_id"], job["message_id"])

    def test_unknown_cannot_be_downgraded_by_stop_or_failed_force_validation(self):
        self.fake.failure = "timeout"
        job = self.prepare()
        self.assertEqual(self.service.send(job["job_id"])["status"], "unknown")
        self.service.stop_event.set()
        self.assertEqual(self.service.send(job["job_id"], force=True)["status"], "unknown")
        self.service.stop_event.clear()
        source = self.root / self.receipt["files"]["txt"]["path"]
        original = source.read_bytes()
        source.write_bytes(b"changed")
        self.assertEqual(self.service.send(job["job_id"], force=True)["status"], "unknown")
        source.write_bytes(original)
        self.assertEqual(self.service.send(job["job_id"])["status"], "unknown")
        self.assertEqual(self.fake.calls.count("send"), 1)

    def test_result_identity_must_match_its_archive(self):
        result = {**self.result, "actual_code": "00-001-0002", "code": "1-2"}
        self.assertEqual(self.prepare([result])["status"], "skipped")

    def test_unknown_other_job_blocks_retry_of_older_explicit_failure(self):
        first = self.prepare()
        self.fake.failure = "auth"
        self.assertEqual(self.service.send(first["job_id"])["status"], "failed")
        second = self.prepare()
        self.fake.failure = "timeout"
        self.assertEqual(self.service.send(second["job_id"])["status"], "unknown")
        self.fake.failure = None
        self.assertEqual(self.service.send(first["job_id"])["status"], "unknown")
        self.assertEqual(self.fake.calls.count("send"), 1)

    def test_unknown_other_job_blocks_later_dry_run_send(self):
        first = self.prepare()
        self.fake.failure = "timeout"
        self.assertEqual(self.service.send(first["job_id"])["status"], "unknown")
        second = self.service.deliver([self.result], "Receiver@example.com", dry_run=True)
        self.fake.failure = None
        self.assertEqual(self.service.send(second["job_id"])["status"], "unknown")
        self.assertEqual(self.fake.calls.count("send"), 1)

    def test_dry_run_job_submitted_then_unknown_reserves_for_future_batches(self):
        first = self.service.deliver([self.result], "Receiver@example.com", dry_run=True)
        self.fake.failure = "timeout"
        self.assertEqual(self.service.send(first["job_id"])["status"], "unknown")
        self.fake.failure = None
        self.assertEqual(self.service.deliver([self.result], "Receiver@example.com")["status"], "skipped")
        self.assertEqual(self.fake.calls.count("send"), 1)

    def test_mixed_failed_job_retries_only_card_not_sent_by_other_job(self):
        second = self.second_result()
        first = self.prepare([self.result, second])
        self.fake.failure = "auth"
        self.assertEqual(self.service.send(first["job_id"])["status"], "failed")
        self.fake.failure = None
        self.assertEqual(self.service.deliver([self.result], "Receiver@example.com")["status"], "sent")
        remaining = self.service.send(first["job_id"])
        self.assertEqual(remaining["status"], "sent")
        self.assertNotEqual(remaining["job_id"], first["job_id"])
        self.assertEqual([card["code"] for card in remaining["cards"]], [second["actual_code"]])
        self.assertEqual(len(remaining["results"]), 2)
        self.assertIn(self.receipt["code"], " ".join(remaining["exclusions"]))
        old = json.loads((self.root / first["job_path"]).read_bytes())
        self.assertEqual(old["status"], "skipped")
        self.assertEqual(old["replacement_job_id"], remaining["job_id"])
        self.assertEqual(self.service.send(first["job_id"])["status"], "sent")
        self.assertEqual(self.fake.calls.count("send"), 2)

    def test_mixed_job_all_cards_already_sent_skips_without_connection(self):
        results = [self.result, self.second_result()]
        first = self.prepare(results)
        self.fake.failure = "auth"
        self.service.send(first["job_id"])
        self.fake.failure = None
        self.assertEqual(self.service.deliver(results, "Receiver@example.com")["status"], "sent")
        count = len(self.factory_calls)
        self.assertEqual(self.service.send(first["job_id"])["status"], "skipped")
        self.assertEqual(len(self.factory_calls), count)

    def test_mixed_job_without_sent_overlap_keeps_original_task(self):
        first = self.prepare([self.result, self.second_result()])
        self.fake.failure = "auth"
        self.service.send(first["job_id"])
        self.fake.failure = None
        delivered = self.service.send(first["job_id"])
        self.assertEqual(delivered["status"], "sent")
        self.assertEqual(delivered["job_id"], first["job_id"])
        self.assertEqual(len(delivered["cards"]), 2)

    def test_force_rebuilds_full_task_including_explicitly_repeated_card(self):
        first = self.prepare([self.result, self.second_result()])
        self.fake.failure = "auth"
        self.service.send(first["job_id"])
        self.fake.failure = None
        self.service.deliver([self.result], "Receiver@example.com")
        delivered = self.service.send(first["job_id"], force=True)
        self.assertEqual(delivered["status"], "sent")
        self.assertEqual(len(delivered["cards"]), 2)
        self.assertNotEqual(delivered["job_id"], first["job_id"])
        self.assertNotEqual(delivered["message_id"], first["message_id"])

    def test_residual_sending_recovers_to_unknown_on_restart(self):
        job = self.prepare()
        path = self.root / job["job_path"]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "sending"
        path.write_text(json.dumps(data), encoding="utf-8")
        from highscore_downloader.mailer import MailService
        restarted = MailService(self.root, self.settings, smtp_factory=lambda *a, **k: self.fail("禁止自动重发"))
        self.assertEqual(restarted.latest_job()["status"], "unknown")
        self.assertEqual(restarted.send(job["job_id"])["status"], "unknown")

    def test_latest_job_uses_creation_time_when_same_second_uuid_order_is_reversed(self):
        class Clock(datetime):
            current = datetime(2026, 10, 18, 12, 0, 0, 100, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        with patch("highscore_downloader.mailer.datetime", Clock), patch(
                "highscore_downloader.mailer.uuid.uuid4", side_effect=[SimpleNamespace(hex="f" * 32), SimpleNamespace(hex="0" * 32)]):
            first = self.service.deliver([self.result], "Receiver@example.com", dry_run=True)
            Clock.current = Clock.current.replace(microsecond=200)
            second = self.service.deliver([self.result], "Receiver@example.com", dry_run=True)
            self.assertGreater(first["job_id"], second["job_id"])
            self.assertLess(first["created_at"], second["created_at"])
            self.assertEqual(self.service.latest_job()["job_id"], second["job_id"])

    def test_legacy_job_without_created_at_can_still_be_loaded(self):
        job = self.prepare()
        path = self.root / job["job_path"]
        data = json.loads(path.read_bytes())
        data.pop("created_at")
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.service.latest_job()["job_id"], job["job_id"])

    def test_explicit_smtp_rejection_retry_and_quit_error_after_accept(self):
        job = self.prepare()
        for failure in ("auth", "reject"):
            self.fake.failure = failure
            failed = self.service.send(job["job_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertNotIn("synthetic private", failed["message"])
        self.fake.failure = None
        self.fake.quit_failure = True
        self.assertEqual(self.service.send(job["job_id"])["status"], "sent")

    def test_stop_does_not_start_network_and_dry_run_does_not_reserve(self):
        stop = threading.Event()
        from highscore_downloader.mailer import MailService
        service = MailService(self.root, self.settings, smtp_factory=lambda *a, **k: self.fail("不得连接"), stop_event=stop)
        job = service.prepare([self.result], "Receiver@example.com")
        stop.set()
        self.assertEqual(service.send(job["job_id"])["status"], "stopped")
        self.assertEqual(service.prepare([self.result], "Receiver@example.com")["status"], "stopped")
        stop.clear()
        preview = service.deliver([self.result], "Receiver@example.com", dry_run=True)
        self.assertEqual(preview["status"], "prepared")
        self.assertEqual(service.prepare([self.result], "Receiver@example.com")["status"], "prepared")

    def test_failed_commit_after_server_accept_never_becomes_retryable(self):
        job = self.prepare()
        original = self.service._write_job

        def fail_sent(data):
            if data["status"] in {"sent", "unknown"}:
                raise OSError("synthetic write failure")
            return original(data)
        with patch.object(self.service, "_write_job", side_effect=fail_sent):
            result = self.service.send(job["job_id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.service.latest_job()["status"], "unknown")
        self.assertEqual(self.service.send(job["job_id"])["status"], "unknown")
        self.assertEqual(self.fake.calls.count("send"), 1)

    def test_parallel_jobs_share_reservations_and_sent_receipts(self):
        from highscore_downloader.mailer import MailService
        other = MailService(self.root, self.settings, smtp_factory=lambda *a, **k: self.fake)
        barrier = threading.Barrier(2)
        results = []
        def prepare(service):
            barrier.wait()
            results.append(service.prepare([self.result], "Receiver@example.com"))
        threads = [threading.Thread(target=prepare, args=(service,)) for service in (self.service, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(row["status"] for row in results), ["prepared", "skipped"])


class MailSettingsTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".cache/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="mail-settings-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_address_validation_is_single_ascii_and_preserves_local_part(self):
        from highscore_downloader.mail_settings import validate_email
        self.assertEqual(validate_email("Synthetic@EXAMPLE.COM"), "Synthetic@example.com")
        for address in ("a@example.com\r\nBcc: b@example.com", "a@example.com,b@example.com", "a@example.com; b@example.com",
                        "名字@example.com", "A <a@example.com>", "a..b@example.com", "a@-bad.com", "a@example.com\n"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_email(address)

    def test_dpapi_roundtrip_and_repr_do_not_leak_authorization_code(self):
        from highscore_downloader.mail_settings import MailSettings, load_mail_settings, save_mail_settings
        settings = MailSettings(username="synthetic@example.com", authorization_code="synthetic-test-only", recipient="R@EXAMPLE.com")
        saved = save_mail_settings(self.root, settings)
        text = (self.root / "mail.local.json").read_text(encoding="utf-8")
        self.assertNotIn(settings.authorization_code, text)
        self.assertNotIn(settings.authorization_code, repr(saved))
        loaded = load_mail_settings(self.root)
        self.assertEqual(loaded.authorization_code, settings.authorization_code)
        self.assertEqual(loaded.recipient, "R@example.com")

    def test_missing_and_corrupt_configuration_return_safe_defaults(self):
        from highscore_downloader.mail_settings import load_mail_settings
        self.assertEqual(load_mail_settings(self.root).username, "")
        (self.root / "mail.local.json").write_text('{"private":"synthetic sensitive detail"', encoding="utf-8")
        loaded = load_mail_settings(self.root)
        self.assertEqual(loaded.username, "")
        self.assertIn("读取失败", loaded.message)
        self.assertNotIn("synthetic sensitive", loaded.message)


if __name__ == "__main__":
    unittest.main()
