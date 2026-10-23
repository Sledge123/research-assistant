"""HighScore Plus 退出契约：正常关闭、验证进程退出、保留保存提示。"""
import unittest
from unittest.mock import Mock

from highscore_downloader.automation import HighScoreAutomation, OperationStopped


class HighScoreExitTests(unittest.TestCase):
    def adapter(self):
        adapter = HighScoreAutomation(timeout=0.01)
        adapter.app = Mock()
        adapter.main = Mock()
        adapter.main.is_enabled.return_value = True
        adapter._activate_window = Mock()
        adapter.app.is_process_running.side_effect = [True, False]
        return adapter

    def test_normal_close_verifies_process_has_exited(self):
        adapter = self.adapter()
        result = adapter.close_application()
        self.assertEqual(result["status"], "closed")
        adapter.main.post_message.assert_called_once_with(0x0010)
        adapter.app.kill.assert_not_called()

    def test_existing_save_prompt_is_preserved(self):
        adapter = self.adapter()
        adapter.main.is_enabled.return_value = False
        result = adapter.close_application()
        self.assertEqual(result["status"], "needs_attention")
        adapter.main.post_message.assert_not_called()
        adapter.app.kill.assert_not_called()

    def test_save_prompt_after_close_is_not_dismissed(self):
        adapter = self.adapter()
        adapter.app.is_process_running.side_effect = None
        adapter.app.is_process_running.return_value = True
        adapter.main.is_enabled.side_effect = [True, False]
        result = adapter.close_application()
        self.assertEqual(result["status"], "needs_attention")
        adapter.main.post_message.assert_called_once_with(0x0010)
        adapter.app.kill.assert_not_called()

    def test_timeout_does_not_force_termination(self):
        adapter = self.adapter()
        adapter.timeout = 0
        adapter.app.is_process_running.side_effect = None
        adapter.app.is_process_running.return_value = True
        result = adapter.close_application()
        self.assertEqual(result["status"], "needs_attention")
        adapter.app.kill.assert_not_called()

    def test_stop_before_close_sends_no_close_request(self):
        adapter = self.adapter()
        adapter.stop_event.set()
        with self.assertRaises(OperationStopped):
            adapter.close_application()
        adapter.main.post_message.assert_not_called()

    def test_missing_process_is_not_launched_to_exit_it(self):
        from highscore_downloader.automation import HighScoreNotRunning
        adapter = HighScoreAutomation()
        adapter.connect = Mock(side_effect=HighScoreNotRunning("当前没有运行"))
        self.assertEqual(adapter.close_application()["status"], "not_running")
        adapter.connect.assert_called_once_with(launch_if_missing=False)


if __name__ == "__main__":
    unittest.main()
