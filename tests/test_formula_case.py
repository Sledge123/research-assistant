"""真实mno2失败路径：查询容错、比例保持、元素大小写歧义不能伪装零结果。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from highscore_database.reader import DatabaseReader,DatabaseError,OrderedDatabaseReader
from test_database_reader import fixture
ROOT=Path(__file__).resolve().parents[1]
class FormulaCaseTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(prefix='formula-case-',dir=base);self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
 def reader(self,formulas,source='Case fixture'):
  path=self.root/('source-'+str(len(list(self.root.glob('*.hsrdb'))))+'.hsrdb');fixture(path,count=len(formulas))
  with closing(sqlite3.connect(path)) as db,db:
   db.executemany('UPDATE general_text_content SET c0chemicalformula=?,c4empiricalformula=? WHERE docid=?',[(v,v,i) for i,v in enumerate(formulas,1)])
   db.execute("UPDATE Properties SET Value=? WHERE Key='Description'",(source,))
  return DatabaseReader(path,cache_directory=self.root/'cache')
 def test_lowercase_real_query_recovers_all_ratios_without_changing_sources(self):
  reader=self.reader(['Mn O2','Mn4 O8','O4 Mn2','MnO']);before=reader.path.read_bytes();expected=reader.browse('formula','MnO2')
  for query in ('mno2','MNO2','mnO₂',' mn o2 '):
   with self.subTest(query=query):self.assertEqual(reader.browse('formula',query),expected)
  self.assertEqual(len(expected),3);self.assertEqual(reader.path.read_bytes(),before)
 def test_common_lowercase_formula_uses_actual_matching_composition(self):
  reader=self.reader(['Si O2','Si2.64 O5.28','Zn O','Ca H2 O2'])
  for query,expected in [('sio2',2),('zno',1),('ca(oh)2',1)]:
   with self.subTest(query=query):self.assertEqual(len(reader.browse('formula',query)),expected)
 def test_valid_co_and_co_do_not_merge_and_ambiguous_lowercase_is_explicit(self):
  reader=self.reader(['C O','Co','CO2'])
  self.assertEqual([r['chemical_formula'] for r in reader.browse('formula','CO')],['C O'])
  self.assertEqual([r['chemical_formula'] for r in reader.browse('formula','Co')],['Co'])
  with self.assertRaisesRegex(DatabaseError,'大小写|多种|歧义'):reader.browse('formula','co')
 def test_ambiguity_across_different_sources_is_not_silently_merged(self):
  reader=OrderedDatabaseReader([self.reader(['Co'],'One'),self.reader(['C O'],'Two')])
  with self.assertRaisesRegex(DatabaseError,'大小写|多种|歧义'):reader.browse_content('formula','co','diffraction')
 def test_variables_and_incomplete_formula_are_not_guessed(self):
  reader=self.reader(['Mn O2','(Fe,Mg)O','MnO2-x'])
  for query in ('mno2-δ','mno2-x','mn(o2','mno0'):
   with self.subTest(query=query):self.assertEqual(reader.browse('formula',query),[])
  self.assertEqual([r['chemical_formula'] for r in reader.browse('formula','MnO2-x')],['MnO2-x'])
if __name__=='__main__':unittest.main()
