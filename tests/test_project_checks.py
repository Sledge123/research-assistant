import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_project_files.ps1"
NAMES = ("PROJECT_PLAN.md", "AGENTS.md", "README.md", "TASKS.md")


class FourFileCheckTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".cache" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="four-files-", dir=base)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT))
        self.addCleanup(self.temp.cleanup)

    def run_check(self):
        return subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
             "-ProjectRoot", str(self.folder)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )

    def fill(self):
        for name in NAMES:
            (self.folder / name).write_text("已填写项目依据\n", encoding="utf-8")

    def test_four_nonempty_files_pass(self):
        self.fill()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_missing_files_are_all_reported(self):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        for name in NAMES:
            self.assertIn("FAIL " + name, result.stdout)

    def test_whitespace_is_not_a_document(self):
        self.fill()
        (self.folder / "README.md").write_text("\n\t  \n", encoding="utf-8")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("README.md : 内容为空", result.stdout)

    def test_directory_cannot_replace_file(self):
        self.fill()
        target = self.folder / "TASKS.md"
        target.unlink()
        target.mkdir()
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TASKS.md : 不是文件", result.stdout)


if __name__ == "__main__":
    unittest.main()
