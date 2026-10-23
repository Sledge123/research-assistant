"""队列测试使用假适配器，不操作任何 Windows 窗口。"""
import tempfile
import threading
import unittest
from pathlib import Path

from highscore_downloader.automation import AutomationError, CardNotFound, OperationStopped
from highscore_downloader.service import BatchRunner
from test_parser import SYNTHETIC

ROOT = Path(__file__).resolve().parent.parent


class FakeAdapter:
    calls = []
    modes = {}

    def __init__(self, executable=None, **kwargs):
        self.stop_event = kwargs["stop_event"]
        self.progress = kwargs["progress"]
        self.request = None

    def load_reference(self, request):
        self.request = request
        self.calls.append(request.query)
        mode = self.modes.get(request.query)
        if mode == "not_found":
            raise CardNotFound("明确未命中")
        if mode == "error":
            raise AutomationError("未知弹窗")
        if mode == "stop":
            self.stop_event.set()
        return [{"code": request.canonical_hint or "00-054-0001", "index": 0, "source": None}]

    def open_reference(self, row_index=0):
        self.index = row_index

    def read_detail(self):
        actual = "01-054-0001" if self.modes.get(self.request.query) == "wrong_complete" else self.request.query
        return {"text": SYNTHETIC.replace("00-001-0001", actual), "wavelength": "1.54060"}

    def export_reference(self, path):
        # 合成 RTF 仅用于队列行为测试。
        text = self.read_detail()["text"]
        encoded = "".join(character if ord(character) < 128 else rf"\u{ord(character)}?" for character in text)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes((r"{\rtf1\ansi " + encoded.replace("\n", r"\par ").replace("\t", r"\tab ") + "}").encode("ascii"))
        return Path(path)

    def close_detail(self):
        if self.modes.get(self.request.query) == "close_stop":
            self.stop_event.set()
            raise OperationStopped("关闭前停止")
        if self.modes.get(self.request.query) == "close_error":
            raise AutomationError("原生详情无法关闭")


class QueueTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / ".cache" / "tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        FakeAdapter.calls = []
        FakeAdapter.modes = {}
        self.events = []
        self.stop = threading.Event()

    def runner(self):
        return BatchRunner(self.root, on_event=self.events.append, stop_event=self.stop,
                           adapter_factory=FakeAdapter)

    def test_duplicate_and_second_run_skip_only_verified_success(self):
        result = self.runner().run("1-1\n00-001-0001")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(FakeAdapter.calls, ["00-001-0001"])
        result = self.runner().run("1-1")
        self.assertEqual(result[0]["status"], "skipped")
        self.assertEqual(FakeAdapter.calls, ["00-001-0001"])
        self.assertTrue(all(not Path(path).is_absolute() for path in result[0]["files"].values()))

    def test_not_found_continues_to_next_card(self):
        FakeAdapter.modes["00-001-0001"] = "not_found"
        result = self.runner().run("1-1\n1-2")
        self.assertEqual([r["status"] for r in result], ["not_found", "success"])

    def test_unknown_dialog_pauses_remaining_items(self):
        FakeAdapter.modes["00-001-0001"] = "error"
        result = self.runner().run("1-1\n1-2")
        self.assertEqual([r["status"] for r in result], ["failed", "pending"])
        self.assertEqual(len(FakeAdapter.calls), 1)

    def test_stop_does_not_commit_current_card(self):
        FakeAdapter.modes["00-001-0001"] = "stop"
        result = self.runner().run("1-1\n1-2")
        self.assertEqual([r["status"] for r in result], ["stopped", "pending"])
        self.assertFalse((self.root / "output/cards/00-001-0001/receipt.json").exists())

    def test_invalid_input_never_runs_automation(self):
        with self.assertRaises(ValueError):
            self.runner().run("../bad")
        self.assertFalse(FakeAdapter.calls)

    def test_candidate_full_identity_is_checked_for_unknown_legacy(self):
        FakeAdapter.modes["054-0001"] = "wrong_complete"
        result = self.runner().run("54-1")
        self.assertEqual(result[0]["status"], "failed")
        self.assertFalse((self.root / "output/cards/01-054-0001/receipt.json").exists())

    def test_stop_after_commit_keeps_success(self):
        FakeAdapter.modes["00-001-0001"] = "close_stop"
        result = self.runner().run("1-1\n1-2")
        self.assertEqual([record["status"] for record in result], ["success", "pending"])
        self.assertTrue(result[0].get("needs_attention"))

    def test_warning_after_commit_keeps_archive_and_requires_attention(self):
        FakeAdapter.modes["00-001-0001"] = "close_error"
        result = self.runner().run("1-1")
        self.assertEqual(result[0]["status"], "success")
        self.assertTrue(result[0].get("needs_attention"))
        self.assertTrue((self.root / "output/cards/00-001-0001/receipt.json").is_file())


if __name__ == "__main__":
    unittest.main()
