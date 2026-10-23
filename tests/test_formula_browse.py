"""新候选检索：真实失败写法的合成回归，原库与旧接口保持。"""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from highscore_database.formula import formula_key
from highscore_database.reader import DatabaseReader, DatabaseError, OrderedDatabaseReader, QueryTooBroad, load_reader
from test_database_reader import fixture

ROOT = Path(__file__).resolve().parents[1]


class FormulaKeyTests(unittest.TestCase):
    def test_exact_element_ratios_spacing_subscripts_order_and_decimals(self):
        for left, right in [('MnO2','Mn O₂'),('MnO2','O8.00 Mn4.00'),('SiO2','Si2.64 O5.28'),
                            ('ZnO','O Zn'),('Ca(OH)2','Ca H2 O2'),('Mg3[Si2O5](OH)4','Mg3Si2O9H4')]:
            with self.subTest(left=left,right=right):self.assertEqual(formula_key(left),formula_key(right))
        self.assertNotEqual(formula_key('Mn4.00O4.00'),formula_key('MnO2'))
        self.assertNotEqual(formula_key('MnO2'),formula_key('MnO2.00000000001'))

    def test_source_hydrate_separator_fractional_water_and_decimal_point(self):
        self.assertEqual(formula_key('Cu3 ( P O4 )2 !3 H2 O'),formula_key('Cu3(PO4)2·3H2O'))
        self.assertEqual(formula_key('Sr(C2H3O2)2·0.5H2O'),formula_key('Sr C4 H7 O4.5'))
        self.assertEqual(formula_key('CuSO4*5H2O'),formula_key('CuSO4·5H2O'))
        self.assertNotEqual(formula_key('CuSO4.5H2O'),formula_key('CuSO4·5H2O'))

    def test_unknown_scientific_syntax_is_not_guessed(self):
        for value in ['Fe+3O(OH)','(Fe,Mg)O','Al2O3·xH2O','Cu2-xS','MnO2-δ','MnO0','NotAnElement','Mn(O2','MnO2·']:
            with self.subTest(value=value):self.assertIsNone(formula_key(value))


class FormulaBrowseTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='formula-browse-',dir=base)
        self.root=Path(self.temp.name);self.addCleanup(self.cleanup)

    def cleanup(self):
        self.root.resolve().relative_to(ROOT.resolve())
        self.temp.cleanup()

    def reader(self, formulas, *, source='Synthetic 数据库'):
        path=self.root/('source-'+str(len(list(self.root.glob('*.hsrdb'))))+'.hsrdb')
        fixture(path,count=len(formulas))
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany('UPDATE general_text_content SET c0chemicalformula=?,c4empiricalformula=? WHERE docid=?',
                           [(formula,formula,index) for index,formula in enumerate(formulas,1)])
            db.execute('UPDATE Properties SET Value=? WHERE Key=\'Description\'',(source,))
        return DatabaseReader(path,cache_directory=self.root/'cache')

    def test_mno2_original_failure_now_matches_all_ratios_without_changing_source(self):
        reader=self.reader(['Mn O2','Mn4.00 O8.00','O4 Mn2','Mn4.00 O4.00'])
        before=reader.path.read_bytes()
        self.assertEqual(reader.search('formula','MnO2'),[])
        with patch.object(reader,'card',side_effect=AssertionError('候选不得解析完整卡片')):
            result=reader.browse('formula','MnO₂')
        self.assertEqual([item['chemical_formula'] for item in result],['Mn O2','Mn4.00 O8.00','O4 Mn2'])
        self.assertEqual(reader.path.read_bytes(),before)
        self.assertEqual(reader.browse('formula','MnO2'),result)
        self.assertEqual(reader.search('formula','MnO2'),[])
        self.assertTrue(all(item['peak_count']==2 and item['space_group']=='P1' for item in result))
        self.assertTrue((self.root/'cache').is_dir())
        self.assertEqual(sorted(path.suffix for path in reader.path.parent.glob('*.hsrdb')),['.hsrdb'])

    def test_more_than_100_candidates_are_all_returned(self):
        reader=self.reader(['Si O2']*121)
        with self.assertRaises(QueryTooBroad):reader.search('formula','Si O2')
        self.assertEqual(len(reader.browse('formula','SiO2')),121)

    def test_literal_fallback_preserves_unknown_formula_without_broadening(self):
        reader=self.reader(['(Fe, Mg)O','Fe O','Mg O'])
        self.assertEqual([item['chemical_formula'] for item in reader.browse('formula',' (Fe,Mg) O ')],['(Fe, Mg)O'])
        self.assertEqual(len(reader.browse('formula','FeO')),1)
        self.assertEqual(reader.browse('formula','MnO2'),[])

    def test_codes_batch_metadata_and_content_namespace(self):
        reader=self.reader(['Mn O2','Zn O'])
        with patch.object(reader,'card',side_effect=AssertionError('候选不得读取完整卡片')):
            result=reader.browse('codes','09-999-0002, 099990001\nICSD-1')
        self.assertEqual([item['code'] for item in result],['09-999-0001','09-999-0002'])
        self.assertTrue(all(item['content_type']=='diffraction' and item['exportable'] for item in result))
        with self.assertRaises(DatabaseError):reader.browse('codes','无效编号')

    def test_cache_tampering_and_source_changes_fail_instead_of_no_result(self):
        reader=self.reader(['Mn O2'])
        reader.browse('formula','MnO2')
        cache=reader._formula_index.path
        with closing(sqlite3.connect(cache)) as db, db:db.execute("UPDATE keys SET record_id='999' WHERE kind='ratio'")
        with self.assertRaises(DatabaseError):reader.browse('formula','MnO2')
        other=DatabaseReader(reader.path,cache_directory=self.root/'cache')
        with self.assertRaises(DatabaseError):other.browse('formula','MnO2')
        with reader.path.open('ab') as stream:stream.write(b'changed')
        with self.assertRaises(DatabaseError):reader.browse('codes','09-999-0001')

    def test_stop_and_cache_outside_project_are_rejected(self):
        reader=self.reader(['Mn O2']);reader.stop_event.set()
        with self.assertRaises(DatabaseError):reader.browse('formula','MnO2')
        reader.stop_event.clear()
        other=DatabaseReader(reader.path,cache_directory=ROOT.parent/'outside-cache')
        with self.assertRaises(DatabaseError):other.browse('formula','MnO2')
        self.assertFalse((ROOT.parent/'outside-cache').exists())

    def test_ordered_new_browse_aggregates_same_codes_and_old_search_stops_first(self):
        first=self.reader(['Mn O2'],source='First')
        second=self.reader(['Mn O2'],source='Second')
        ordered=OrderedDatabaseReader([first,second])
        self.assertEqual([item['source'] for item in ordered.search('formula','Mn O2')],['First'])
        self.assertEqual([item['source'] for item in ordered.browse_content('formula','MnO2','diffraction')],['First','Second'])
        with patch.object(second,'browse',side_effect=DatabaseError('模拟断线')):
            with self.assertRaises(DatabaseError):ordered.browse_content('formula','MnO2','diffraction')

    def test_load_reader_explicit_relative_config_and_cache(self):
        reader=self.reader(['Mn O2']);config={'path':reader.path.name}
        (self.root/'four.local.json').write_text(json.dumps(config),encoding='utf-8')
        loaded=load_reader(self.root,config_relative='four.local.json',cache_directory='isolated-cache')
        self.assertEqual(len(loaded.browse('formula','MnO2')),1)
        self.assertEqual(loaded._formula_index.directory,self.root/'isolated-cache')
        with self.assertRaises(DatabaseError):load_reader(self.root,config_relative='../not-allowed.json')


class JadeBrowseTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='jade-browse-',dir=base)
        self.root=Path(self.temp.name);self.addCleanup(self.cleanup)

    def cleanup(self):
        self.root.resolve().relative_to(ROOT.resolve())
        self.temp.cleanup()

    def test_jade_index_metadata_all_121_without_raw_card(self):
        from test_jade_index_reader import synthetic_source_files
        from highscore_database.jade_index_build import build_index
        from highscore_database.jade_index import JadeIndexReader, FORMAT
        source=self.root/'source';source.mkdir()
        for name,data in synthetic_source_files(count=121).items():(source/name).write_bytes(data)
        with patch('highscore_database.jade_index_build.EXPECTED_CARDS',121):
            receipt=build_index(self.root,'source','index.sqlite3','receipt.json')
        with patch('highscore_database.jade_index.EXPECTED_CARDS',121):
            reader=JadeIndexReader(self.root/'index.sqlite3',profile={'format':receipt['format'],'source':'Test JADE','sha256':receipt['sha256']},cache_directory=self.root/'cache')
        with self.assertRaises(QueryTooBroad):reader.search('formula','MnO2')
        with patch.object(reader,'card',side_effect=AssertionError('不能读取完整卡片')):
            result=reader.browse('formula','Mn4.00O8.00')
            codes=reader.browse('codes','30-0820\nICSD-1')
        self.assertEqual(len(result),121);self.assertEqual(len(codes),1)
        self.assertEqual(result[0]['mineral_name'],'Synthetic mineral')
        self.assertEqual(result[0]['peak_count'],2)

    def test_structure_unsupported_candidate_is_visible_without_exporting(self):
        from test_jade_structure_reader import fixture_source
        from highscore_database.jade_structure_build import build_database
        from highscore_database.jade_structure import JadeStructureReader, FORMAT
        source=fixture_source(self.root/'source');target=self.root/'structure.sqlite3'
        build_database(source,target)
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('UPDATE structures SET formula=?,unsupported=?',('Mn O2',json.dumps(['原始占有率超出有效范围'])))
            db.execute('UPDATE metadata SET value=? WHERE key=\'unsupported_count\'',('1',))
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        reader=JadeStructureReader(target,profile={'format':FORMAT,'source':'Test structure','sha256':digest},cache_directory=self.root/'cache')
        with patch.object(reader,'card',side_effect=AssertionError('不能导出结构')):
            result=reader.browse('formula','MnO2')
            self.assertEqual(reader.browse('codes','ICSD-1 00-030-0820'),result)
        self.assertEqual(len(result),1);self.assertFalse(result[0]['exportable'])
        self.assertIn('占有率',result[0]['support_message']);self.assertEqual(result[0]['site_count'],9)
        self.assertEqual(result[0]['content_type'],'structure')


if __name__=='__main__':unittest.main()
