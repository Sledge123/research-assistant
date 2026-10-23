"""验证可读卡片信息与原生栏目对应，技术审计数据不混入报告正文。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from striprtf.striprtf import rtf_to_text
from highscore_database.reader import DatabaseReader
from highscore_database.exporter import DatabaseExporter, report_text, load_report_settings
from highscore_database.reporting import REPORT_VERSION
from test_database_reader import fixture

ROOT = Path(__file__).resolve().parents[1]


class ReportLayoutTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=cache, prefix='report-layout-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'synthetic.hsrdb'
        fixture(self.database)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE general_indexed SET XTSLSYS='C',QUALFINAL='S',XTLASPECTN=227,DM=5.18,DX=5.20,XTLVOL=591.86,Z=8,IIC=4.9")
            db.execute("UPDATE general_stored SET CAS='synthetic-CAS',Comment=?", ('Color: Blue. See \\ITJournal\\RG, \\BF32\\RG; D#c#a#l#c and \\Li2 Mn O3\\.\r\r',))
            db.execute("INSERT INTO Subfiles VALUES (1,'Inorganic')")
            db.execute('INSERT INTO SubFileRef VALUES (1,1)')
        self.reader = DatabaseReader(self.database)

    def test_body_has_readable_native_sections_and_all_available_metadata(self):
        text = report_text(self.reader.card('09-999-0001'))
        for value in ('名称与化学式', '晶体学参数', '子库与质量', '注释', '参考文献', '峰表',
                      'Synthetic mineral', '立方', '227', '5.18', '5.20', '591.86', '4.90',
                      'Inorganic', 'Star (S)', 'synthetic-CAS', 'Synthetic author', 'Synthetic journal'):
            self.assertIn(value, text)
        for value in ('数据库 SHA256', '原始字段', '"docid"', '"ProductID"', '"c0volume"', '\\IT', '\\RG', '\\BF', 'D#c#a#l#c'):
            self.assertNotIn(value, text)
        self.assertIn('Dcalc', text)
        self.assertIn('Li2MnO3', text)

    def test_body_excludes_requested_footer_and_color_without_removing_source_data(self):
        card=self.reader.card('09-999-0001')
        text=report_text(card)
        for value in ('来源：','数据库生成报告','非 HighScore 原生导出','显示数值','CSV / JSON','颜色：','Color: Blue'):
            self.assertNotIn(value,text)
        self.assertIn('See Journal',text)
        self.assertEqual(card['database_raw']['text']['c5color'],'Blue')
        self.assertIn('Color: Blue',card['comments'])

    def test_styled_rtf_roundtrip_and_full_precision_data_are_preserved(self):
        output = self.root / 'report'
        output.mkdir()
        receipt = DatabaseExporter(output, self.reader).export('09-999-0001')
        raw = (output / receipt['files']['rtf']['path']).read_bytes().decode('ascii')
        self.assertIn('Tahoma', raw)
        self.assertIn('Courier New', raw)
        self.assertIn('\\b\\ul\\fs24', raw)
        self.assertIn('\\tx3000', raw)
        card = json.loads((output / receipt['files']['json']['path']).read_text(encoding='utf-8'))
        decoded = rtf_to_text(raw).encode('utf-16', 'surrogatepass').decode('utf-16')
        self.assertEqual(decoded, card['raw_text'])
        self.assertEqual(receipt['report_version'], REPORT_VERSION)
        self.assertEqual(card['report_fields']['mineral_name'], 'Synthetic mineral')
        self.assertEqual(card['verification']['completeness_scope'], 'available_database_fields')
        source = self.reader.card('09-999-0001')
        self.assertEqual([{k:v for k,v in p.items() if k!='two_theta_deg'} for p in card['peaks']],
                         [{k:v for k,v in p.items() if k!='two_theta_deg'} for p in source['peaks']])
        self.assertEqual(card['database_raw'], source['database_raw'])
        self.assertIn('1.23457', card['raw_text'])
        self.assertIn(source['peaks'][1]['d_angstrom'], (output / receipt['files']['csv']['path']).read_text(encoding='utf-8-sig'))
        self.assertEqual(card['wavelength_angstrom'], '1.54060')
        self.assertTrue(all(row['two_theta_deg'] is not None for row in card['peaks']))
        self.assertTrue(all(row['two_theta_deg'] is None for row in source['peaks']))

    def test_missing_or_unknown_values_do_not_become_invented_metadata(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE general_indexed SET XTSLSYS='?',QUALFINAL='?',DM=-1,DX=-1,XTLVOL=0,Z=0,IIC=0")
        text = report_text(DatabaseReader(self.database).card('09-999-0001'))
        self.assertNotIn('1970', text)
        self.assertIn('未识别（?）', text)
        self.assertIn('未提供', text)
        self.assertNotIn('Measured density', text)

    def test_explicit_wavelength_restores_angles_without_changing_source_blobs(self):
        settings={'wavelength_angstrom':'1.54060','source':'synthetic 测试波长'}
        (self.root/'database-report.local.json').write_text(json.dumps(settings),encoding='utf-8')
        self.assertEqual(load_report_settings(self.root),settings)
        output=self.root/'with-angle';output.mkdir()
        receipt=DatabaseExporter(output,self.reader,report_settings=settings).export('09-999-0001')
        card=json.loads((output/receipt['files']['json']['path']).read_text(encoding='utf-8'))
        source=self.reader.card('09-999-0001')
        self.assertEqual(card['wavelength_angstrom'],'1.54060')
        self.assertEqual(card['angular_conversion']['two_theta_origin'],'calculated')
        self.assertEqual(card['peaks'][0]['two_theta_deg'],'35.892')
        self.assertEqual(card['database_raw'],source['database_raw'])
        self.assertTrue(all(row['two_theta_deg'] is None for row in source['peaks']))
        self.assertIn('2θ [°]',card['raw_text'])
        self.assertNotIn('不是独立测量角度',card['raw_text'])
        self.assertEqual(DatabaseExporter(output,self.reader,report_settings=settings)
                         .export('09-999-0001'),receipt)

    def test_unreachable_angles_stay_null_and_invalid_wavelength_is_rejected(self):
        output=self.root/'unreachable';output.mkdir()
        receipt=DatabaseExporter(output,self.reader,report_settings={
            'wavelength_angstrom':'10','source':'synthetic'}).export('09-999-0001')
        card=json.loads((output/receipt['files']['json']['path']).read_text(encoding='utf-8'))
        self.assertTrue(all(row['two_theta_deg'] is None for row in card['peaks']))
        self.assertEqual(card['peak_count'],2)
        from highscore_database.reader import DatabaseError
        for value in ('nan','inf','-1','0'):
            (self.root/'database-report.local.json').write_text(json.dumps({
                'wavelength_angstrom':value,'source':'synthetic'}),encoding='utf-8')
            with self.assertRaises(DatabaseError):load_report_settings(self.root)


if __name__ == '__main__':
    unittest.main()
