"""本地配置必须可迁移，损坏配置应提供中文提示。"""

import json
from pathlib import Path
import tempfile
import queue
import threading
import unittest
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".cache" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="settings-", dir=base)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def module(self):
        try:
            from highscore_downloader import settings
        except ImportError:
            self.fail("尚未实现本地配置模块")
        return settings

    def test_absent_config_uses_auto_discovery_without_writing(self):
        settings = self.module().load_settings(self.root)
        self.assertIsNone(settings.executable_relative)
        self.assertTrue(settings.auto_discover)
        self.assertFalse((self.root / "config.local.json").exists())

    def test_relative_executable_round_trip(self):
        module = self.module()
        executable = self.root / "tools" / "HighScorePlus.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"test")
        module.save_settings(self.root, executable)
        saved = json.loads((self.root / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["highscore_executable"], "tools/HighScorePlus.exe")
        self.assertNotIn(str(self.root), json.dumps(saved))
        loaded = module.load_settings(self.root)
        self.assertEqual(loaded.executable_relative, "tools/HighScorePlus.exe")
        self.assertEqual(module.resolve_executable(self.root, loaded), executable)

    def test_relative_input_is_resolved_from_project_root(self):
        module = self.module()
        saved = module.save_settings(self.root, Path("tools/HighScorePlus.exe"))
        self.assertEqual(module.resolve_executable(self.root, saved),
                         self.root / "tools" / "HighScorePlus.exe")

    def test_relative_path_survives_project_move(self):
        module = self.module()
        settings = module.save_settings(self.root, Path("tools/HighScorePlus.exe"))
        moved = self.root / "moved-project"
        self.assertEqual(module.resolve_executable(moved, settings),
                         moved / "tools" / "HighScorePlus.exe")

    def test_external_same_drive_location_is_still_relative(self):
        module = self.module()
        executable = self.root.parent / "HighScorePlus.exe"
        saved = module.save_settings(self.root, executable)
        self.assertEqual(saved.executable_relative, "../HighScorePlus.exe")
        self.assertEqual(module.resolve_executable(self.root, saved), executable)

    def test_cross_drive_selection_saves_discovery_information_only(self):
        module = self.module()
        executable = self.root / "private-user" / "HighScorePlus.exe"
        with patch.object(module.os.path, "relpath", side_effect=ValueError("different drive")):
            settings = module.save_settings(self.root, executable)
        text = (self.root / "config.local.json").read_text(encoding="utf-8")
        self.assertNotIn("private-user", text)
        self.assertIsNone(settings.executable_relative)
        self.assertTrue(settings.auto_discover)
        self.assertIn("自动发现", settings.message)
        self.assertIn("重新选择", settings.message)
        self.assertIsNone(module.resolve_executable(self.root, module.load_settings(self.root)))

    def test_clear_selection_restores_auto_discovery(self):
        module = self.module()
        module.save_settings(self.root, Path("tools/HighScorePlus.exe"))
        module.save_settings(self.root, None)
        settings = module.load_settings(self.root)
        self.assertIsNone(settings.executable_relative)
        self.assertTrue(settings.auto_discover)

    def test_corrupt_config_is_reported_and_not_overwritten(self):
        module = self.module()
        config = self.root / "config.local.json"
        config.write_text("{损坏", encoding="utf-8")
        settings = module.load_settings(self.root)
        self.assertTrue(settings.auto_discover)
        self.assertIn("配置", settings.message)
        self.assertIn("失败", settings.message)
        self.assertEqual(config.read_text(encoding="utf-8"), "{损坏")

    def test_non_object_and_invalid_paths_are_rejected(self):
        module = self.module()
        for value in ([], {"highscore_executable": 4},
                      {"highscore_executable": "C:/Users/test/HighScorePlus.exe"},
                      {"highscore_executable": "C:HighScorePlus.exe"},
                      {"highscore_executable": "/tools/HighScorePlus.exe"},
                      {"highscore_executable": "tools/notepad.exe"}):
            with self.subTest(value=value):
                config = self.root / "config.local.json"
                config.write_text(json.dumps(value), encoding="utf-8")
                settings = module.load_settings(self.root)
                self.assertIsNone(settings.executable_relative)
                self.assertTrue(settings.auto_discover)
                self.assertIn("失败", settings.message)

    def test_invalid_program_name_does_not_write_configuration(self):
        module = self.module()
        with self.assertRaisesRegex(ValueError, "HighScorePlus.exe"):
            module.save_settings(self.root, Path("tools/notepad.exe"))
        self.assertFalse((self.root / "config.local.json").exists())

    def test_config_directory_is_reported(self):
        module = self.module()
        (self.root / "config.local.json").mkdir()
        settings = module.load_settings(self.root)
        self.assertIn("失败", settings.message)
        with self.assertRaises(ValueError):
            module.save_settings(self.root, None)


class GuiQueueContractTests(unittest.TestCase):
    def test_retry_only_includes_unfinished_requests(self):
        from highscore_downloader.gui import _retry_text

        records = [{"request": f"19-{index}", "status": status}
                   for index, status in enumerate(
                       ("success", "skipped", "failed", "stopped", "pending", "not_found", "running"), 1)]
        self.assertEqual(_retry_text(records), "19-3\n19-4\n19-5\n19-6")

    def test_source_request_waits_for_main_thread_response(self):
        from highscore_downloader.gui import HighScoreWindow

        application = HighScoreWindow.__new__(HighScoreWindow)
        application.events = queue.Queue()
        application.stop_event = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(application._choose_source([{"code": "19-1"}])))
        worker.start()
        kind, request = application.events.get(timeout=1)
        self.assertEqual(kind, "source")
        self.assertTrue(worker.is_alive())
        request["selection"] = 0
        request["ready"].set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])

    def test_stop_releases_background_source_wait(self):
        from highscore_downloader.gui import HighScoreWindow

        application = HighScoreWindow.__new__(HighScoreWindow)
        application.events = queue.Queue()
        application.stop_event = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(application._choose_source([])))
        worker.start()
        application.events.get(timeout=1)
        application.stop_event.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [None])


if __name__ == "__main__":
    unittest.main()
