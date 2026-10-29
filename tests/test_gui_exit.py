"""退出行为：任务完成后退出；异常保留窗口；手动退出等待后台结束。"""
import queue
import threading
import unittest
from unittest.mock import Mock

from highscore_downloader.gui import HighScoreWindow
from highscore_downloader.numbers import parse_code


class GuiExitTests(unittest.TestCase):
    def application(self, codes=("19-0629",), *, auto_exit=True):
        application = HighScoreWindow.__new__(HighScoreWindow)
        application.window = Mock()
        application.events = queue.Queue()
        application.stop_event = threading.Event()
        application.running = True
        application.closing = False
        application.auto_exit_var = Mock()
        application.auto_exit_var.get.return_value = auto_exit
        application.status_var = Mock()
        application.stop_button = Mock()
        application.records = {}
        for code in codes:
            request = parse_code(code)
            application.records[request.dedupe_key] = {
                "request": code, "code": request.query, "status": "pending"}
        application.active_keys = list(application.records)
        application._update_row = Mock()
        application._set_running_controls = Mock()
        application._answer_source = Mock()
        application._append_log = Mock()
        application._exit_highscore = Mock()
        return application

    def complete(self, application, statuses, **values):
        results = [{"code": application.records[key]["code"], "status": status, **values}
                   for key, status in zip(application.active_keys, statuses)]
        application.events.put(("done", results))
        application._drain_events()

    def test_enabled_option_exits_highscore_and_keeps_archive_results(self):
        application = self.application(("19-0629", "00-025-1376"))
        self.complete(application, ("success", "skipped"))
        application._exit_highscore.assert_called_once_with()
        application.window.destroy.assert_not_called()
        self.assertFalse(application.running)

    def test_unchecked_option_keeps_successful_results_visible(self):
        application = self.application(auto_exit=False)
        self.complete(application, ("success",))
        application.window.destroy.assert_not_called()
        application._exit_highscore.assert_not_called()
        self.assertFalse(application.closing)

    def test_unfinished_or_failed_batch_keeps_window_open(self):
        for status in ("failed", "not_found", "stopped", "pending", "running"):
            with self.subTest(status=status):
                application = self.application(("19-0629", "00-025-1376"))
                self.complete(application, ("success", status))
                application.window.destroy.assert_not_called()
                application._exit_highscore.assert_not_called()

    def test_background_error_keeps_window_open(self):
        application = self.application()
        application.events.put(("failed", "后台处理异常"))
        application._drain_events()
        application.window.destroy.assert_not_called()
        application._exit_highscore.assert_not_called()

    def test_stop_request_after_success_does_not_auto_exit(self):
        application = self.application()
        application.stop_event.set()
        self.complete(application, ("success",))
        application.window.destroy.assert_not_called()
        application._exit_highscore.assert_not_called()

    def test_warning_after_committed_archive_keeps_window_open(self):
        application = self.application()
        self.complete(application, ("success",), needs_attention=True)
        application.window.destroy.assert_not_called()
        application._exit_highscore.assert_not_called()

    def test_empty_batch_never_auto_exits(self):
        application = self.application(())
        self.complete(application, ())
        application.window.destroy.assert_not_called()
        application._exit_highscore.assert_not_called()

    def test_highscore_exit_result_keeps_archive_window_open(self):
        for status in ("closed", "not_running", "needs_attention"):
            with self.subTest(status=status):
                application = self.application()
                application.events.put(("highscore_exit_done", {"status": status, "message": "退出结果"}))
                application._drain_events()
                self.assertFalse(application.running)
                application.window.destroy.assert_not_called()
                application.status_var.set.assert_called_with("退出结果")

    def test_manual_exit_when_idle_closes_immediately(self):
        application = self.application()
        application.running = False
        application._close()
        application.window.destroy.assert_called_once_with()

    def test_manual_exit_during_work_waits_for_finished_event(self):
        application = self.application(auto_exit=False)
        application._close()
        self.assertTrue(application.stop_event.is_set())
        application.window.destroy.assert_not_called()
        self.complete(application, ("stopped",))
        application.window.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
