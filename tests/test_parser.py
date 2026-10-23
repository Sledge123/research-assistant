"""只用显式人工样本测试解析，不把它作为真实取卡证据。"""
import unittest

from highscore_downloader.parser import CardParseError, parse_card_text, parse_rtf


SYNTHETIC = """Name and formula
Reference code:\t00-001-0001
Compound name:\tSynthetic 水
Empirical formula:\tH2O
Chemical formula:\tH2O
Crystallographic parameters
Crystal system:\tCubic
a (?):\t4.12340
b (?):\t4.12340
c (?):\t4.12340
Alpha (°):\t90.0000
Beta (°):\t90.0000
Gamma (°):\t90.0000
Subfiles and quality
Quality:\tTest
Comments
Note:\t人工测试数据
Note:\t重复注释需保留
References
Primary reference:\tSynthetic reference
Peak list
No.\th\tk\tl\td [A]\t2Theta[deg]\tI [%]
1\t1\t0\t0\t4.12340\t21.000\t100.0
2\t\t\t\t2.06170\t\t12.50
Stick Pattern
"""


class ParserTests(unittest.TestCase):
    def test_preserves_precision_and_missing_columns(self):
        card = parse_card_text(SYNTHETIC, wavelength="1.54060", expected_code="1-1")
        self.assertTrue(card["complete"])
        self.assertEqual(card["peak_count"], 2)
        self.assertEqual(card["peaks"][0]["d_angstrom"], "4.12340")
        self.assertIsNone(card["peaks"][1]["h"])
        self.assertIsNone(card["peaks"][1]["two_theta_deg"])
        self.assertEqual(card["cell_parameters"]["a_angstrom"], "4.12340")
        self.assertEqual(card["wavelength_angstrom"], "1.54060")
        self.assertIn("重复注释", card["comments"])
        self.assertEqual(card["chemical_formula"], "H2O")

    def test_pipe_table_from_rtf_is_supported(self):
        text = SYNTHETIC.replace("\t", "|")
        card = parse_card_text(text, wavelength="1.54060")
        self.assertEqual(card["peaks"][1]["intensity_percent"], "12.50")

    def test_native_fixed_width_table_with_blank_indices(self):
        start = SYNTHETIC.index("No.\t")
        end = SYNTHETIC.index("Stick Pattern")
        fixed = "No.    h    k    l      d [A]     2Theta[deg] I [%]   \n"
        fixed += f"{1:>5}{10:>5}{0:>5}{-2:>5}{'4.12340':>13}{'21.000':>13}{'100.0':>5}\n"
        fixed += f"{2:>5}{'':>15}{'2.06170':>13}{'':>13}{'12.50':>5}\n"
        card = parse_card_text(SYNTHETIC[:start] + fixed + SYNTHETIC[end:])
        self.assertEqual(card["peaks"][0]["h"], "10")
        self.assertEqual(card["peaks"][0]["l"], "-2")
        self.assertIsNone(card["peaks"][1]["two_theta_deg"])

    def test_wrong_reference_rejected_even_if_comments_mention_request(self):
        with self.assertRaises(CardParseError):
            parse_card_text(SYNTHETIC + "Additional Pattern: 00-019-0629", expected_code="19-0629")

    def test_missing_peak_row_cannot_be_complete(self):
        with self.assertRaises(CardParseError):
            parse_card_text(SYNTHETIC.replace("2\t\t", "3\t\t"))

    def test_malformed_peak_cannot_be_complete(self):
        with self.assertRaises(CardParseError):
            parse_card_text(SYNTHETIC.replace("4.12340\t21.000", "oops\t21.000"))

    def test_missing_optional_sections_are_empty(self):
        text = SYNTHETIC[:SYNTHETIC.index("Crystallographic parameters")] + SYNTHETIC[SYNTHETIC.index("Peak list"):]
        card = parse_card_text(text)
        self.assertIsNone(card["cell_parameters"]["a_angstrom"])
        self.assertEqual(card["comments"], "")
        self.assertEqual(card["references"], "")

    def test_missing_peak_section_rejected(self):
        with self.assertRaises(CardParseError):
            parse_card_text(SYNTHETIC[:SYNTHETIC.index("Peak list")])

    def test_empty_optional_field_does_not_consume_next_line(self):
        text = SYNTHETIC.replace("Empirical formula:\tH2O", "Empirical formula:\t")
        card = parse_card_text(text)
        self.assertIsNone(card["empirical_formula"])
        self.assertEqual(card["chemical_formula"], "H2O")

    def test_non_rtf_is_rejected(self):
        with self.assertRaises(CardParseError):
            parse_rtf(b"plain text named rtf")


if __name__ == "__main__":
    unittest.main()
