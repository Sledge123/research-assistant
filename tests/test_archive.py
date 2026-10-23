"""归档存储使用显式 synthetic 样例，不包含真实卡片数据。"""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from highscore_downloader.numbers import parse_code


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PEAK_FIELDS = ["no", "h", "k", "l", "d_angstrom", "two_theta_deg", "intensity_percent"]


def synthetic_card():
    return {
        "code": "09-999-0001",
        "source": "synthetic 测试来源",
        "phase_name": "合成测试相（非真实卡片）",
        "chemical_formula": "X2 Y",
        "raw_text": "synthetic 测试样例\n合成测试相，精度应原样保存。\n",
        "peak_count": 2,
        "peaks": [
            {"no": "1", "h": "-1", "k": "0", "l": "2",
             "d_angstrom": "1.234567890123456789", "two_theta_deg": "123.4567890123456789",
             "intensity_percent": "100.0000"},
            {"no": "2", "h": None, "k": None, "l": None,
             "d_angstrom": "2.000000000000000001", "two_theta_deg": None,
             "intensity_percent": "0.0000"},
        ],
        "wavelength_angstrom": "1.5406000000000000",
        "complete": True,
        "metadata": {"说明": "显式 synthetic", "列表": [None, "保留", 3]},
    }


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        try:
            from highscore_downloader.archive import ArchiveError, ArchiveStore
        except ImportError:
            self.fail("尚未实现归档存储模块")
        self.ArchiveError = ArchiveError
        base = PROJECT_ROOT / ".cache" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="archive-", dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文归档根"
        self.root.mkdir()
        self.store = ArchiveStore(self.root)
        self.card = synthetic_card()
        self.rtf = self.root / ".cache" / "input" / "synthetic.rtf"
        self.rtf.parent.mkdir(parents=True)
        self.rtf.write_bytes(b"{\\rtf1\\ansi synthetic test data}")

    def save(self, card=None):
        return self.store.save(self.card if card is None else card, self.rtf, "09-999-0001")

    def receipt_path(self, receipt):
        return self.root / receipt["receipt_path"]

    def rewrite_receipt(self, receipt):
        self.receipt_path(receipt).write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    def rewrite_payload(self, receipt, kind, payload):
        path = self.root / receipt["files"][kind]["path"]
        path.write_bytes(payload)
        receipt["files"][kind]["size"] = len(payload)
        receipt["files"][kind]["sha256"] = hashlib.sha256(payload).hexdigest()
        self.rewrite_receipt(receipt)

    def test_saves_chinese_paths_precision_metadata_and_relative_receipts(self):
        receipt = self.save()
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["receipt_path"], "output/cards/09-999-0001/receipt.json")
        self.assertEqual(set(receipt["files"]), {"rtf", "txt", "json", "csv"})
        for kind, item in receipt["files"].items():
            self.assertEqual(item["path"], f"output/cards/09-999-0001/09-999-0001.{kind}")
            self.assertFalse(Path(item["path"]).is_absolute())
            payload = (self.root / item["path"]).read_bytes()
            self.assertEqual(item["size"], len(payload))
            self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
        json_bytes = (self.root / receipt["files"]["json"]["path"]).read_bytes()
        self.assertIn("合成测试相".encode("utf-8"), json_bytes)
        self.assertEqual(json.loads(json_bytes), self.card)
        self.assertEqual((self.root / receipt["files"]["txt"]["path"]).read_text(encoding="utf-8"),
                         self.card["raw_text"])
        csv_bytes = (self.root / receipt["files"]["csv"]["path"]).read_bytes()
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        self.assertEqual(rows[0], PEAK_FIELDS)
        self.assertEqual(rows[1][4], "1.234567890123456789")
        self.assertEqual(rows[1][6], "100.0000")
        self.assertEqual(rows[2][1:4], ["", "", ""])
        self.assertEqual(self.store.validate(Path(receipt["receipt_path"])), receipt)

    def test_reuses_valid_archive_without_overwriting_or_requiring_new_rtf(self):
        first = self.save()
        before = {kind: (self.root / item["path"]).stat().st_mtime_ns
                  for kind, item in first["files"].items()}
        self.rtf.unlink()
        self.assertEqual(self.save(), first)
        self.assertEqual(before, {kind: (self.root / item["path"]).stat().st_mtime_ns
                                  for kind, item in first["files"].items()})

    def test_rejects_different_source_without_overwriting_success(self):
        first = self.save()
        other = copy.deepcopy(self.card)
        other["source"] = "另一个 synthetic 来源"
        with self.assertRaises(self.ArchiveError):
            self.save(other)
        self.assertEqual(self.store.validate(self.receipt_path(first)), first)

    def test_continues_directory_containing_only_same_native_rtf(self):
        directory = self.root / "output" / "cards" / self.card["code"]
        directory.mkdir(parents=True)
        native = directory / f"{self.card['code']}.rtf"
        original = self.rtf.read_bytes()
        native.write_bytes(original)
        before = native.stat().st_mtime_ns
        receipt = self.store.save(self.card, native, self.card["code"])
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(native.read_bytes(), original)
        self.assertEqual(native.stat().st_mtime_ns, before)
        self.assertEqual(self.store.validate(self.receipt_path(receipt)), receipt)

    def test_native_continuation_cannot_adopt_another_rtf_or_partial_output(self):
        directory = self.root / "output" / "cards" / self.card["code"]
        directory.mkdir(parents=True)
        native = directory / f"{self.card['code']}.rtf"
        native.write_bytes(self.rtf.read_bytes())
        with self.assertRaises(self.ArchiveError):
            self.save()
        (directory / "partial.txt").write_text("synthetic 未完成输出", encoding="utf-8")
        with self.assertRaises(self.ArchiveError):
            self.store.save(self.card, native, self.card["code"])

    def test_rejects_incomplete_empty_identity_or_inconsistent_peaks(self):
        mutations = [
            {"complete": False}, {"complete": "True"}, {"code": "999-1"},
            {"code": "../09-999-0001"}, {"phase_name": " \t"}, {"raw_text": ""},
            {"peaks": []}, {"peak_count": 1}, {"peak_count": True},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                card = {**copy.deepcopy(self.card), **mutation}
                with self.assertRaises(self.ArchiveError):
                    self.save(card)
        self.assertFalse((self.root / "output" / "cards" / self.card["code"] / "receipt.json").exists())

    def test_rejects_missing_invalid_or_nonstring_required_peak_values(self):
        for field, values in {
            "d_angstrom": [None, "", "NaN", "Infinity", "0", "-1", "bad", 1.2345],
            "intensity_percent": [None, "", "NaN", "Infinity", "-1", "101", 100.0],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    card = copy.deepcopy(self.card)
                    card["peaks"][0][field] = value
                    with self.assertRaises(self.ArchiveError):
                        self.save(card)

    def test_rejects_empty_or_nonrtf_native_card(self):
        for payload in (b"", b"plain text", b"\xef\xbb\xbf{\\rtf1}"):
            with self.subTest(payload=payload):
                self.rtf.write_bytes(payload)
                with self.assertRaises(self.ArchiveError):
                    self.save()

    def test_validate_detects_changed_bytes_for_every_file(self):
        receipt = self.save()
        for kind, item in receipt["files"].items():
            with self.subTest(kind=kind):
                path = self.root / item["path"]
                original = path.read_bytes()
                path.write_bytes(original + b"corrupt")
                with self.assertRaises(self.ArchiveError):
                    self.store.validate(self.receipt_path(receipt))
                path.write_bytes(original)

    def test_validate_checks_json_identity_after_hash_is_updated(self):
        receipt = self.save()
        other = copy.deepcopy(self.card)
        other["code"] = "08-999-0001"
        self.rewrite_payload(receipt, "json", json.dumps(other, ensure_ascii=False).encode("utf-8"))
        with self.assertRaises(self.ArchiveError):
            self.store.validate(self.receipt_path(receipt))

    def test_validate_checks_peak_count_after_hash_is_updated(self):
        receipt = self.save()
        other = copy.deepcopy(self.card)
        other["peak_count"] = 3
        self.rewrite_payload(receipt, "json", json.dumps(other, ensure_ascii=False).encode("utf-8"))
        with self.assertRaises(self.ArchiveError):
            self.store.validate(self.receipt_path(receipt))

    def test_validate_checks_csv_values_after_hash_is_updated(self):
        receipt = self.save()
        csv_bytes = (self.root / receipt["files"]["csv"]["path"]).read_bytes()
        self.rewrite_payload(receipt, "csv", csv_bytes.replace(b"1.234567890123456789", b"1.2"))
        with self.assertRaises(self.ArchiveError):
            self.store.validate(self.receipt_path(receipt))

    def test_validate_checks_text_against_json_after_hash_is_updated(self):
        receipt = self.save()
        self.rewrite_payload(receipt, "txt", "更改后的 synthetic 文本".encode("utf-8"))
        with self.assertRaises(self.ArchiveError):
            self.store.validate(self.receipt_path(receipt))

    def test_receipt_file_paths_cannot_escape_project_root(self):
        receipt = self.save()
        for bad_path in ("../outside.rtf", "/outside.rtf", "C:/outside.rtf", "output/../outside.rtf"):
            with self.subTest(path=bad_path):
                changed = copy.deepcopy(receipt)
                changed["files"]["rtf"]["path"] = bad_path
                self.rewrite_receipt(changed)
                with self.assertRaises(self.ArchiveError):
                    self.store.validate(self.receipt_path(receipt))

    def test_input_rtf_and_receipt_paths_cannot_escape_project_root(self):
        outside = Path(self.temporary.name) / "outside.rtf"
        outside.write_bytes(b"{\\rtf1 outside synthetic data}")
        for source in (outside, Path("..") / "outside.rtf"):
            with self.subTest(source=str(source)):
                with self.assertRaises(self.ArchiveError):
                    self.store.save(self.card, source, "09-999-0001")
        with self.assertRaises(self.ArchiveError):
            self.store.validate(Path("..") / "receipt.json")

    def test_reuse_still_rejects_input_path_outside_project_root(self):
        self.save()
        with self.assertRaises(self.ArchiveError):
            self.store.save(self.card, Path("..") / "outside.rtf", "09-999-0001")

    def test_find_success_uses_matching_identity(self):
        receipt = self.save()
        self.assertEqual(self.store.find_success(parse_code("999-1")), receipt)
        self.assertEqual(self.store.find_success(parse_code("09-999-0001")), receipt)
        self.assertIsNone(self.store.find_success(parse_code("08-999-0001")))

    def test_unknown_legacy_cannot_silently_choose_between_multiple_full_codes(self):
        first = self.save()
        other = copy.deepcopy(self.card)
        other["code"] = "08-999-0001"
        other["source"] = "另一 synthetic 来源"
        self.store.save(other, self.rtf, other["code"])
        with self.assertRaisesRegex(self.ArchiveError, "歧义"):
            self.store.find_success(parse_code("999-1"))
        self.assertEqual(self.store.find_success(parse_code("09-999-0001")), first)

    def test_find_success_does_not_skip_corrupt_or_missing_receipt(self):
        receipt = self.save()
        path = self.root / receipt["files"]["rtf"]["path"]
        path.write_bytes(b"corrupt")
        with self.assertRaises(self.ArchiveError):
            self.store.find_success(parse_code("999-1"))
        self.receipt_path(receipt).unlink()
        with self.assertRaises(self.ArchiveError):
            self.store.find_success(parse_code("999-1"))

    def test_save_does_not_overwrite_damaged_existing_archive(self):
        receipt = self.save()
        path = self.root / receipt["files"]["rtf"]["path"]
        path.write_bytes(b"corrupt")
        with self.assertRaises(self.ArchiveError):
            self.save()
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_failed_payload_write_never_publishes_success_receipt(self):
        import os
        replace = os.replace

        def fail_csv(source, destination):
            if Path(destination).suffix == ".csv":
                raise OSError("synthetic 磁盘写入失败")
            return replace(source, destination)

        with patch("highscore_downloader.archive.os.replace", side_effect=fail_csv):
            with self.assertRaises(self.ArchiveError):
                self.save()
        self.assertFalse((self.root / "output" / "cards" / self.card["code"] / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
