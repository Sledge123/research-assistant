import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app

ROOT = Path(__file__).resolve().parent.parent


class CommandTests(unittest.TestCase):
    def test_validate_detects_missing_receipt(self):
        cache = ROOT / ".cache" / "tests"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as name:
            root = Path(name)
            directory = root / "output/cards/00-001-0001"
            directory.mkdir(parents=True)
            (directory / "00-001-0001.rtf").write_bytes(br"{\rtf1 synthetic}")
            output = io.StringIO()
            with patch.object(app, "ROOT", root), contextlib.redirect_stdout(output):
                result = app.main(["--validate"])
            self.assertEqual(result, 1)
            self.assertIn("FAIL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
