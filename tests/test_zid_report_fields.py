"""v5显示ZID已解析科学字段，历史v4字节与缺失值保持。"""
import copy
import struct
import unittest
from highscore_database import reporting, reporting_v4
from highscore_database.jade_index import decode_zid
from test_jade_index_reader import make_card_fixture


class ZidReportFieldsTests(unittest.TestCase):
    def card(self):
        card = make_card_fixture()
        metadata = decode_zid(struct.pack('<8B11f8s', 1, 3, 59, 2, 77, 4, 4, 0,
            2., 3.32, 2.19, 177.69, 14., 11.48, 4.36, 3.55, 90., 90., 90., b'Pmmn    '))
        card.update(index_metadata=metadata, cell_parameters=metadata['cell_parameters'], space_group=metadata['space_group'])
        return card

    def test_v5_reads_available_zid_fields_without_inventing_density_kind(self):
        fields = reporting.report_fields(self.card())
        self.assertEqual(fields['crystal_system'], '正交')
        self.assertEqual(fields['space_group_number'], '59')
        self.assertEqual(fields['z'], '2.00')
        self.assertEqual(fields['rir'], '2.19')
        self.assertEqual(fields['cell_volume'], '177.69')
        self.assertEqual(fields['density_raw'], '3.32')
        self.assertIsNone(fields['density_measured'])
        self.assertIsNone(fields['density_calculated'])
        text = reporting.report_text(self.card())
        for value in ('Pmmn', '11.4800', '4.3600', '3.5500', '177.69', '3.32', '2.19'):
            self.assertIn(value, text)
        self.assertIn('密度（原值）', text)

    def test_old_v4_rendering_does_not_change_even_when_current_parser_supports_zid(self):
        card = self.card(); card['verification'] = {'report_version': 4}
        self.assertEqual(reporting.report_rtf(card), reporting_v4.report_rtf(card))
        self.assertEqual(reporting.report_fields(card), reporting_v4.report_fields(card))

    def test_v1_missing_metadata_remains_missing(self):
        fields = reporting.report_fields(make_card_fixture())
        self.assertEqual(fields['crystal_system'], '未提供')
        self.assertIsNone(fields['space_group_number'])
        self.assertIsNone(fields['cell']['a'])


if __name__ == '__main__': unittest.main()
