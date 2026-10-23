"""GUI/CLI 邮件编排离线验证，不启动 HighScore，不连接 SMTP。"""
import contextlib
import io
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import app
from highscore_downloader.archive import ArchiveStore
from highscore_downloader.gui import HighScoreWindow
from highscore_downloader.mail_settings import MailSettings
from test_archive import synthetic_card

ROOT = Path(__file__).resolve().parents[1]


class MailWorkflowTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".cache/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="mail-workflow-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = MailSettings(username="synthetic@example.com", authorization_code="synthetic-test-only")
        self.guard = patch.object(socket, "create_connection", side_effect=AssertionError("离线测试禁止外网"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def run_cli(self, argv, results, mail_result=None, settings=None):
        output = io.StringIO()
        with patch.object(app, "ROOT", self.root), patch("highscore_downloader.service.BatchRunner") as runner, \
                patch("highscore_downloader.mail_settings.load_mail_settings", return_value=settings or self.settings), \
                patch("highscore_downloader.mailer.MailService") as mailer, contextlib.redirect_stdout(output):
            runner.return_value.run.return_value = results
            mailer.return_value.deliver.return_value = mail_result or {"status": "prepared", "dry_run": True}
            code = app.main(argv)
        return code, output.getvalue(), runner, mailer

    def test_cli_requires_explicit_recipient_for_mail(self):
        results = [{"status": "success"}]
        code, text, runner, mailer = self.run_cli(["--codes", "19-0629"], results)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), results)
        mailer.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            app.main(["--mail-dry-run", "--codes", "19-0629"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_mail_configuration_rejected_before_rpa(self):
        for address, settings in (("", self.settings), ("a@example.com\r\nBcc:b@example.com", self.settings),
                                  ("recipient@example.com", MailSettings())):
            with self.subTest(address=address):
                with patch.object(app, "ROOT", self.root), patch("highscore_downloader.service.BatchRunner") as runner, \
                        patch("highscore_downloader.mail_settings.load_mail_settings", return_value=settings), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    app.main(["--codes", "19-0629", "--email-to", address])
                runner.assert_not_called()

    def test_cli_dry_run_has_independent_mail_and_archive_results(self):
        results = [{"status": "success", "code": "19-0629"}, {"status": "failed", "code": "25-1376"}]
        code, text, runner, mailer = self.run_cli(
            ["--codes", "19-0629,25-1376", "--email-to", "R@EXAMPLE.com", "--mail-dry-run"], results)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["results"], results)
        self.assertEqual(json.loads(text)["mail"]["status"], "prepared")
        mailer.return_value.deliver.assert_called_once_with(results, "R@example.com", dry_run=True)

    def test_cli_mail_failed_unknown_exit_nonzero_archive_stays_success(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status):
                code, text, _, _ = self.run_cli(["--codes", "19-0629", "--email-to", "r@example.com"],
                                              [{"status": "success"}], {"status": status})
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(text)["results"][0]["status"], "success")

    def test_gui_offline_uses_only_existing_input_archives(self):
        native = self.root / "synthetic.rtf"
        native.write_bytes(br"{\rtf1 synthetic}")
        receipt = ArchiveStore(self.root).save(synthetic_card(), native, "09-999-0001")
        window = HighScoreWindow.__new__(HighScoreWindow)
        window.project_root = self.root
        window.input_text = Mock()
        window.input_text.get.return_value = "09-999-0001,00-001-0002"
        with patch("highscore_downloader.service.BatchRunner") as runner:
            results = window._existing_mail_results()
        self.assertEqual(results[0]["status"], "skipped")
        self.assertEqual(results[0]["receipt_path"], receipt["receipt_path"])
        self.assertEqual(results[1]["status"], "failed")
        runner.assert_not_called()

    def test_gui_mail_event_is_independent_and_before_done(self):
        window = HighScoreWindow.__new__(HighScoreWindow)
        window.events = queue.Queue()
        window.window = Mock()
        window.closing = False
        order = []
        window._apply_mail_result = lambda result: order.append(("mail", result["status"]))
        window._finished = lambda results, error: order.append(("done", results[0]["status"]))
        window.events.put(("mail_result", {"status": "unknown"}))
        window.events.put(("done", [{"status": "success"}]))
        window._drain_events()
        self.assertEqual(order, [("mail", "unknown"), ("done", "success")])

    def test_gui_worker_keeps_initial_mail_snapshot_and_emits_mail_before_done(self):
        window = HighScoreWindow.__new__(HighScoreWindow)
        window.project_root = self.root
        window.running = window.closing = False
        window.mail_settings = self.settings
        window.mail_enabled_var = Mock()
        window.mail_enabled_var.get.return_value = True
        window.mail_recipient_var = Mock()
        window.mail_recipient_var.get.return_value = "First@EXAMPLE.com"
        window.executable = self.root / "HighScorePlus.exe"
        window.executable.write_bytes(b"synthetic executable placeholder")
        window.records = {}
        window.table = Mock()
        window.table.get_children.return_value = ()
        window.events = queue.Queue()
        window._update_row = window._set_running_controls = window._append_log = window._choose_source = Mock()
        window.status_var = Mock()

        class InlineThread:
            def __init__(self, *, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        results = [{"code": "19-0629", "status": "success"}]

        def changed_while_running(*args, **kwargs):
            window.mail_recipient_var.get.return_value = "Changed@example.com"
            window.mail_settings = MailSettings(username="changed@example.com", authorization_code="different-synthetic")
            return results

        with patch("highscore_downloader.service.BatchRunner") as runner, \
                patch("highscore_downloader.gui.MailService") as mailer, \
                patch("highscore_downloader.gui.threading.Thread", InlineThread):
            runner.return_value.run.side_effect = changed_while_running
            mailer.return_value.deliver.return_value = {"status": "failed", "message": "synthetic failure"}
            window._begin("19-0629", retry=False)
        self.assertEqual(mailer.call_args.args[1].username, self.settings.username)
        mailer.return_value.deliver.assert_called_once_with(results, "First@example.com")
        events = [window.events.get_nowait(), window.events.get_nowait()]
        self.assertEqual([kind for kind, _ in events], ["mail_result", "done"])
        self.assertEqual(events[1][1], results)

    def test_gui_snapshot_is_normalized_and_unknown_retry_requires_choice(self):
        window = HighScoreWindow.__new__(HighScoreWindow)
        window.mail_settings = self.settings
        window.mail_enabled_var = Mock()
        window.mail_enabled_var.get.return_value = True
        window.mail_recipient_var = Mock()
        window.mail_recipient_var.get.return_value = "R@EXAMPLE.COM"
        settings, recipient = window._mail_snapshot()
        self.assertEqual(recipient, "R@example.com")
        self.assertIsNot(settings, self.settings)
        window.running = window.closing = False
        window.window = Mock()
        window.latest_mail = {"job_id": "synthetic", "status": "unknown"}
        window._launch_mail_only = Mock()
        with patch("highscore_downloader.gui.messagebox.askyesno", return_value=False) as ask:
            window._retry_mail()
        ask.assert_called_once()
        window._launch_mail_only.assert_not_called()


if __name__ == "__main__":
    unittest.main()
