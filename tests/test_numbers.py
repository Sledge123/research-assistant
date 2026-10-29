"""卡片编号解析、匹配与批量输入规则。"""

from dataclasses import FrozenInstanceError
import unittest


class ReferenceCodeTests(unittest.TestCase):
    def parse(self, text):
        try:
            from highscore_downloader.numbers import parse_code
        except ImportError:
            self.fail("尚未实现卡片编号解析")
        return parse_code(text)

    def parse_many(self, text, limit=20):
        try:
            from highscore_downloader.numbers import parse_codes
        except ImportError:
            self.fail("尚未实现批量卡片编号解析")
        return parse_codes(text, limit=limit)

    def test_legacy_number_maps_to_database_zero(self):
        code = self.parse("19-629")
        self.assertEqual(code.original, "19-629")
        self.assertEqual(code.query, "00-019-0629")
        self.assertEqual(code.canonical_hint, "00-019-0629")
        self.assertEqual(code.dedupe_key, "00-019-0629")

    def test_legacy_mapping_boundaries(self):
        for number in (1, 53):
            with self.subTest(number=number):
                self.assertEqual(self.parse(f"{number}-1").canonical_hint,
                                 f"00-{number:03d}-0001")
        for number in (70, 89):
            with self.subTest(number=number):
                self.assertEqual(self.parse(f"{number}-1").canonical_hint,
                                 f"01-{number:03d}-0001")
        for number in (54, 69, 90, 123):
            with self.subTest(number=number):
                code = self.parse(f"{number}-1")
                self.assertIsNone(code.canonical_hint)
                self.assertEqual(code.query, f"{number:03d}-0001")

    def test_explicit_database_is_preserved(self):
        code = self.parse("01-019-0629")
        self.assertEqual(code.query, "01-019-0629")
        self.assertEqual(code.canonical_hint, "01-019-0629")
        self.assertEqual(self.parse("000190629").query, "00-019-0629")
        self.assertEqual(self.parse("2-3-4").query, "02-003-0004")

    def test_layout_is_normalized(self):
        code = self.parse("  00 – 019 – 0629  ")
        self.assertEqual(code.query, "00-019-0629")
        self.assertEqual(self.parse("１９－６２９").query, "00-019-0629")

    def test_reference_code_is_frozen(self):
        code = self.parse("19-629")
        with self.assertRaises(FrozenInstanceError):
            code.query = "00-019-0001"

    def test_canonical_matching_checks_all_three_fields(self):
        code = self.parse("00-019-0629")
        self.assertTrue(code.matches("000190629"))
        self.assertTrue(code.matches("19-629"))
        self.assertFalse(code.matches("01-019-0629"))
        self.assertFalse(code.matches("00-020-0629"))
        self.assertFalse(code.matches("00-019-0630"))
        self.assertFalse(code.matches("错误编号"))

    def test_unknown_legacy_matches_only_set_and_entry(self):
        code = self.parse("54-1")
        self.assertTrue(code.matches("00-054-0001"))
        self.assertTrue(code.matches("32-054-0001"))
        self.assertTrue(code.matches("054-0001"))
        self.assertFalse(code.matches("00-055-0001"))
        self.assertFalse(code.matches("00-054-0002"))
        self.assertFalse(self.parse("00-054-0001").matches("54-1"))

    def test_invalid_inputs_have_chinese_errors_and_original_input(self):
        for value in ("", " ", "../19-0629", "19/0629", "19\\0629", "C:19-0629",
                      "0019-0629", "19-00629", "000-019-0629", "00-0019-0629",
                      "00-019-00629", "00019062", "0000190629", "abc", "19--629"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as context:
                    self.parse(value)
                self.assertIn("编号", str(context.exception))
                self.assertIn(repr(value), str(context.exception))

    def test_batch_separators_order_and_known_legacy_deduplication(self):
        codes = self.parse_many("19-629,00-019-0629，70-1;01-070-0001；54-2\n55-3\t56-4 57-5")
        self.assertEqual([code.query for code in codes],
                         ["00-019-0629", "01-070-0001", "054-0002", "055-0003",
                          "056-0004", "057-0005"])
        self.assertEqual(codes[0].original, "19-629")

    def test_batch_keeps_unknown_legacy_and_explicit_databases_separate(self):
        codes = self.parse_many("54-1 054-0001 00-054-0001 01-054-0001")
        self.assertEqual(len(codes), 3)
        self.assertEqual(len({code.dedupe_key for code in codes}), 3)

    def test_batch_accepts_spaces_around_dashes(self):
        codes = self.parse_many("00 - 019 - 0629, 54 - 1")
        self.assertEqual([code.query for code in codes], ["00-019-0629", "054-0001"])

    def test_batch_limit_counts_unique_codes(self):
        self.assertEqual(len(self.parse_many("19-629 00-019-0629 000190629", limit=1)), 1)
        with self.assertRaisesRegex(ValueError, "上限.*1"):
            self.parse_many("19-629 19-630", limit=1)
        values = " ".join(f"19-{index}" for index in range(1, 22))
        with self.assertRaisesRegex(ValueError, "上限.*20"):
            self.parse_many(values)

    def test_batch_rejects_any_invalid_item_and_empty_input(self):
        with self.assertRaises(ValueError) as context:
            self.parse_many("19-629,错误编号,70-1")
        self.assertIn("错误编号", str(context.exception))
        with self.assertRaises(ValueError):
            self.parse_many("\n , ， ; ； \t")
        with self.assertRaises(ValueError):
            self.parse_many("19-629", limit=0)


if __name__ == "__main__":
    unittest.main()
