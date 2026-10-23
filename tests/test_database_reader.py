"""SQLite 数据库报告回归；所有常规测试仅使用 synthetic 数据。"""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


def fixture(path, *, count=2, lines=None, hkl=None):
    connection = sqlite3.connect(path)
    connection.executescript('''
    CREATE TABLE Properties(ID INTEGER PRIMARY KEY, Key TEXT, Value TEXT);
    CREATE TABLE general_indexed(id INTEGER PRIMARY KEY, ProductID TEXT, XTSLSYS TEXT, STATUS INTEGER,
      A REAL,B REAL,C REAL,ALPHA REAL,BETA REAL,GAMMA REAL,A_OVER_C REAL,Z INTEGER,DM REAL,DX REAL,D REAL,
      QUALFINAL TEXT,IIC REAL,NumberOfElements INTEGER,XTLASPECTN INTEGER,XTLVOL REAL,CTIME INTEGER,MTIME INTEGER);
    CREATE TABLE general_text_content(docid INTEGER PRIMARY KEY,c0chemicalformula,c1compoundname,c2mineralname,
      c3commonname,c4empiricalformula,c5color);
    CREATE TABLE general_stored(pid INTEGER PRIMARY KEY,CELLED TEXT,SPGR TEXT,SPGRED TEXT,ANX REAL,
      XTLSG TEXT,XTLSGED TEXT,Comment TEXT,CAS TEXT,Lines BLOB,HKL BLOB);
    CREATE TABLE literature_map(doc INTEGER PRIMARY KEY,pid INTEGER);
    CREATE TABLE literature_text_content(docid INTEGER PRIMARY KEY,c0volume,c1pages,c2year,c3authors,c4journal,c5referencetype);
    CREATE TABLE Subfiles(ID INTEGER PRIMARY KEY,SubFile TEXT);
    CREATE TABLE SubFileRef(pid INTEGER,SubFileID INTEGER);
    ''')
    connection.executemany('INSERT INTO Properties(Key,Value) VALUES (?,?)',
                           [('ID','PDF2'),('Description','Synthetic 数据库'),('FormatVersion','2.3'),('NoOfPatterns',str(count))])
    for index in range(1,count+1):
        connection.execute('INSERT INTO general_indexed(id,ProductID,A,B,C,ALPHA,BETA,GAMMA) VALUES (?,?,?,?,?,?,?,?)',
                           (index,f'09999{index:04d}',1.25,2.5,3.75,90,90,90))
        connection.execute('INSERT INTO general_text_content VALUES (?,?,?,?,?,?,?)',
                           (index,'X +2 Y','Synthetic compound','Synthetic mineral','','X Y','Blue'))
        connection.execute('INSERT INTO general_stored(pid,SPGR,Comment,Lines,HKL) VALUES (?,?,?,?,?)',
                           (index,'P1','Synthetic 注释',lines if lines is not None else struct.pack('<fHfH',1.234567,100,2.5,50),
                            hkl if hkl is not None else struct.pack('<hhhhhh',-1,0,2,1,2,3)))
        connection.execute('INSERT INTO literature_map VALUES (?,?)',(index,index))
        connection.execute('INSERT INTO literature_text_content VALUES (?,?,?,?,?,?,?)',
                           (index,'V','1-2','2026','Synthetic author','Synthetic journal',4))
    connection.commit()
    connection.close()


class DatabaseReaderTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='database-',dir=base);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.database=self.root/'synthetic.hsrdb';fixture(self.database)

    def reader(self, **kw):
        from highscore_database.reader import DatabaseReader
        return DatabaseReader(self.database, **kw)

    def test_exact_queries_parameterized_and_empirical_formula_is_searchable(self):
        reader=self.reader()
        for mode,value in [('compound_name','Synthetic compound'),('mineral_name','Synthetic mineral'),
                           ('formula','X +2 Y'),('formula','X Y')]:
            self.assertEqual(len(reader.search(mode,value)),2)
        self.assertEqual(reader.search('compound_name',"' OR 1=1 --"),[])
        self.assertEqual(reader.search('formula','XY'),[])
        self.assertEqual(reader.search('compound_name','Synthetic%'),[])

    def test_broad_query_is_rejected_without_silent_truncation(self):
        from highscore_database.reader import QueryTooBroad
        db=self.root/'broad.hsrdb';fixture(db,count=101)
        from highscore_database.reader import DatabaseReader
        with self.assertRaises(QueryTooBroad):DatabaseReader(db).search('compound_name','Synthetic compound')

    def test_reader_never_changes_database_and_uses_content_fingerprint(self):
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        reader=self.reader();card=reader.card('09-999-0001')
        self.assertEqual(card['database_id'],reader.database_id)
        self.assertEqual(reader.sha256,before)
        self.assertEqual(card['source'],'Synthetic 数据库')
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),['synthetic.hsrdb'])

    def test_source_records_include_every_linked_table_and_blob_without_other_cards(self):
        with closing(sqlite3.connect(self.database)) as db,db:
            db.executescript('''
            CREATE TABLE pattern_elements(pid,e1,e2);
            CREATE TABLE pattern_strongestlines(pid,ProductID,Lines BLOB);
            CREATE TABLE general_used(pid,ProductID);
            CREATE TABLE PatternPhase(ID,ReferenceCode,PhaseName);
            CREATE TABLE Phases(ID,Name,Phase BLOB,Peaks BLOB);
            CREATE TABLE PatternScan(ID,ReferenceCode,ScanName);
            CREATE TABLE Scans(ID,Name,Scan BLOB);
            CREATE TABLE DeprecatedPatterns(ID,OldReferenceCode,NewReferenceCode);
            INSERT INTO pattern_elements VALUES (1,12,34),(2,99,88);
            INSERT INTO general_used VALUES (1,'099990001');
            INSERT INTO PatternPhase VALUES (1,'099990001','phase-a'),(2,'099990002','phase-b');
            INSERT INTO PatternScan VALUES (1,'099990001','scan-a'),(2,'099990002','scan-b');
            INSERT INTO DeprecatedPatterns VALUES (1,'099990009','099990001'),(2,'099990008','099990007');
            ''')
            db.execute('INSERT INTO pattern_strongestlines VALUES (1,?,?)',('099990001',b'original-strongest'))
            db.executemany('INSERT INTO Phases VALUES (?,?,?,?)',[(1,'phase-a',b'original-phase',b'original-peaks'),(2,'phase-b',b'other',None)])
            db.executemany('INSERT INTO Scans VALUES (?,?,?)',[(1,'scan-a',b'original-scan'),(2,'scan-b',b'other')])
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        records=self.reader().card('09-999-0001')['database_raw']['source_records']
        self.assertEqual(records['absent_tables'],[])
        self.assertEqual(len(records['tables']),16)
        self.assertEqual(records['tables']['Phases']['rows'][0]['Name'],'phase-a')
        self.assertEqual(len(records['tables']['Phases']['rows']),1)
        self.assertEqual(base64.b64decode(records['tables']['Phases']['rows'][0]['Phase']['blob_base64']),b'original-phase')
        self.assertEqual(base64.b64decode(records['tables']['Scans']['rows'][0]['Scan']['blob_base64']),b'original-scan')
        self.assertEqual(records['tables']['pattern_elements']['rows'],[{'pid':1,'e1':12,'e2':34}])
        self.assertEqual(len(records['tables']['DeprecatedPatterns']['rows']),1)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)

    def test_all_float32_peaks_hkl_raw_order_and_missing_two_theta_are_preserved(self):
        card=self.reader().card('09-999-0001')
        self.assertEqual(card['peak_count'],2)
        self.assertEqual(card['peaks'][0]['d_angstrom'],'2.5')
        self.assertEqual([card['peaks'][1][k] for k in ('h','k','l')],['-1','0','2'])
        self.assertIsNone(card['peaks'][0]['two_theta_deg'])
        self.assertIsNone(card['wavelength_angstrom'])
        self.assertEqual(base64.b64decode(card['database_raw']['lines_base64']),struct.pack('<fHfH',1.234567,100,2.5,50))
        self.assertEqual(card['literature'][0]['c4journal'],'Synthetic journal')
        self.assertEqual(card['comments'],'Synthetic 注释')

    def test_bad_blob_layout_and_intensity_fail_closed(self):
        from highscore_database.reader import DatabaseError
        for name,lines,hkl in [('size',b'x',None),('hkl',None,b'x'),
                               ('negative',struct.pack('<fH',-1,10),struct.pack('<hhh',1,1,1)),
                               ('intensity',struct.pack('<fH',1,101),struct.pack('<hhh',1,1,1)),
                               ('nan',struct.pack('<fH',float('nan'),10),struct.pack('<hhh',1,1,1))]:
            db=self.root/(name+'.hsrdb');fixture(db,lines=lines,hkl=hkl)
            from highscore_database.reader import DatabaseReader
            with self.subTest(name=name),self.assertRaises(DatabaseError):DatabaseReader(db).card('09-999-0001')

    def test_observed_missing_hkl_sentinel_stays_null_and_raw_blob_is_retained(self):
        db=self.root/'missing.hsrdb';raw=struct.pack('<hhhhhh',-128,-128,-128,1,2,3)
        fixture(db,hkl=raw)
        from highscore_database.reader import DatabaseReader
        card=DatabaseReader(db).card('09-999-0001')
        self.assertTrue(all(card['peaks'][1][key] is None for key in ('h','k','l')))
        self.assertEqual(base64.b64decode(card['database_raw']['hkl_base64']),raw)

    def test_pdf2_thousand_scale_uses_native_999_sentinel_and_keeps_original_bytes(self):
        from highscore_database.reader import DatabaseReader
        from highscore_database.validation import refresh_card
        path=self.root/'thousand.hsrdb'
        lines=struct.pack('<fHfHfH',3.5,999,4.5,285,2.0,3)
        hkl=struct.pack('<hhhhhhhhh',1,0,1,0,0,2,3,0,3)
        fixture(path,lines=lines,hkl=hkl)
        card=DatabaseReader(path).card('09-999-0001')
        self.assertEqual([p['intensity_percent'] for p in card['peaks']],['28.5','100','0.3'])
        self.assertEqual([p['database_index'] for p in card['peaks']],[1,0,2])
        self.assertEqual(card['database_raw']['intensity_scale'],999)
        self.assertEqual(card['database_raw']['reader_profile'],'pdf2_2.3')
        self.assertEqual(base64.b64decode(card['database_raw']['lines_base64']),lines)
        self.assertEqual(base64.b64decode(card['database_raw']['hkl_base64']),hkl)
        self.assertEqual(refresh_card(card),card)

    def test_pdf2_thousand_scale_exports_and_revalidates_all_four_formats(self):
        import csv
        from highscore_database.reader import DatabaseReader
        from highscore_database.exporter import DatabaseExporter
        from highscore_downloader.archive import ArchiveStore
        path=self.root/'thousand-export.hsrdb'
        fixture(path,lines=struct.pack('<fHfH',3.5,999,4.5,285))
        output=self.root/'thousand-output';output.mkdir()
        receipt=DatabaseExporter(output,DatabaseReader(path)).export('09-999-0001')
        self.assertEqual(ArchiveStore(output).validate(receipt['receipt_path']),receipt)
        card=json.loads((output/receipt['files']['json']['path']).read_text(encoding='utf-8'))
        with (output/receipt['files']['csv']['path']).open(encoding='utf-8-sig',newline='') as stream:
            peaks=list(csv.DictReader(stream))
        self.assertEqual([p['intensity_percent'] for p in peaks],['28.5','100'])
        self.assertIn('28.5',card['raw_text'])
        self.assertIn('100.0',card['raw_text'])
        DatabaseExporter._verify_rtf(output/receipt['files']['rtf']['path'],card['raw_text'])

    def test_pdf2_thousand_scale_metadata_and_out_of_range_values_fail_closed(self):
        import copy
        from highscore_database.reader import DatabaseReader,DatabaseError
        from highscore_database.validation import refresh_card
        for maximum in (101,998,1000,65535):
            path=self.root/('invalid-scale-'+str(maximum)+'.hsrdb')
            fixture(path,lines=struct.pack('<fHfH',3.5,maximum,4.5,50))
            with self.subTest(maximum=maximum),self.assertRaises(DatabaseError):
                DatabaseReader(path).card('09-999-0001')
        path=self.root/'thousand-metadata.hsrdb'
        fixture(path,lines=struct.pack('<fHfH',3.5,999,4.5,285))
        card=DatabaseReader(path).card('09-999-0001')
        for key,value in (('intensity_scale',100),('reader_profile','cod2024_hs4x')):
            changed=copy.deepcopy(card);changed['database_raw'][key]=value
            with self.subTest(key=key),self.assertRaises(DatabaseError):refresh_card(changed)

    def test_sql_progress_handler_enforces_timeout(self):
        from highscore_database.reader import DatabaseError
        reader=self.reader(timeout=.001)
        with self.assertRaises(DatabaseError):
            with reader.connection() as connection:
                connection.execute('WITH RECURSIVE x(a) AS (VALUES(1) UNION ALL SELECT a+1 FROM x WHERE a<10000000) SELECT sum(a) FROM x').fetchone()

    def test_rtf_unicode_and_control_characters_round_trip_without_injection(self):
        from highscore_database.exporter import rtf_bytes
        from striprtf.striprtf import rtf_to_text
        value='数据库报告 {测试} \\ 峰表\n中文 Ω 😀\t1.25\n'
        # striprtf 保留 UTF-16 代理对；规范化后与支持代理对的 RTF 阅读器结果相同。
        decoded=rtf_to_text(rtf_bytes(value).decode('ascii'))
        self.assertEqual(decoded.encode('utf-16','surrogatepass').decode('utf-16'),value)

    def test_unknown_schema_and_missing_database_description_are_rejected(self):
        from highscore_database.reader import DatabaseError,DatabaseReader
        bad=self.root/'bad.hsrdb';c=sqlite3.connect(bad);c.execute('create table anything(x)');c.close()
        with self.assertRaises(DatabaseError):DatabaseReader(bad)
        c=sqlite3.connect(self.database);c.execute("delete from Properties where Key='Description'");c.commit();c.close()
        with self.assertRaises(DatabaseError):self.reader()

    def test_stop_before_query_prevents_sql_work(self):
        from highscore_database.reader import DatabaseError
        event=threading.Event();reader=self.reader(stop_event=event);event.set()
        with self.assertRaises(DatabaseError):reader.search('compound_name','Synthetic compound')

    def test_export_is_explicit_database_report_and_revalidates_four_formats(self):
        from highscore_database.exporter import DatabaseExporter
        from highscore_downloader.archive import ArchiveStore
        root=self.root/'isolated';root.mkdir()
        reader=self.reader();receipt=DatabaseExporter(root,reader).export('09-999-0001')
        verified=ArchiveStore(root).validate(receipt['receipt_path'])
        card=json.loads((root/verified['files']['json']['path']).read_text(encoding='utf-8'))
        self.assertEqual(card['verification']['method'],'sqlite_readonly_database_report')
        self.assertEqual(receipt['rtf_origin'],'generated_database_report')
        self.assertFalse(card['verification']['native_highscore_export'])
        self.assertNotIn('非 HighScore 原生导出',card['raw_text'])
        self.assertEqual(card['peak_count'],2)
        self.assertEqual(DatabaseExporter(root,reader).export('09-999-0001'),receipt)

    def test_recomputed_hashes_do_not_hide_rtf_text_or_source_peak_changes(self):
        from highscore_database.exporter import DatabaseExporter
        from highscore_downloader.archive import ArchiveError,ArchiveStore
        for kind in ('rtf','json'):
            with self.subTest(kind=kind):
                root=self.root/('changed-'+kind);root.mkdir();reader=self.reader()
                receipt=DatabaseExporter(root,reader).export('09-999-0001')
                target=root/receipt['files'][kind]['path']
                if kind=='rtf':target.write_bytes(target.read_bytes()[:-1]+b' synthetic injected}')
                else:
                    card=json.loads(target.read_text(encoding='utf-8'));card['peaks'][0]['database_index']=99
                    target.write_text(json.dumps(card,ensure_ascii=False),encoding='utf-8')
                receipt['files'][kind].update(size=target.stat().st_size,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
                (root/receipt['receipt_path']).write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8')
                # v4在服务器接收/下载验证时即重解析单卡原记录，不等到执行器复用才拒绝。
                with self.assertRaises(ArchiveError):ArchiveStore(root).validate(receipt['receipt_path'])
                with self.assertRaises(ArchiveError):DatabaseExporter(root,reader).export('09-999-0001')


if __name__=='__main__':unittest.main()
