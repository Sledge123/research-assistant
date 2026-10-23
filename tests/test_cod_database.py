"""COD 的独立格式、强度换算及原始 BLOB 回归；只使用合成库。"""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from test_database_reader import fixture
from highscore_database.reader import DatabaseReader, DatabaseError, load_reader
from highscore_database.exporter import DatabaseExporter
from highscore_web.source_files import original_data
from highscore_web.source_files import download_zip
from highscore_web.artifacts import accept_zip
from highscore_database.executor import package_archive
import io
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class CodDatabaseTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='cod-', dir=cache)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'cod.hsrdb'
        self.lines = struct.pack('<fHfH', 2.5, 1000, 1.25, 125)
        self.lines_i = struct.pack('<ff', 1000.0, 125.25)
        fixture(self.path, count=1, lines=self.lines, hkl=struct.pack('<hhhhhh', 1, 0, 0, 0, 1, 1))
        db = sqlite3.connect(self.path)
        with db:
            db.execute("DELETE FROM Properties WHERE Key IN ('Description','FormatVersion')")
            db.execute("UPDATE Properties SET Value='CIFDB' WHERE Key='ID'")
            db.execute("UPDATE general_indexed SET ProductID='961011259'")
            db.execute('ALTER TABLE general_stored ADD COLUMN LinesI BLOB')
            db.execute('UPDATE general_stored SET LinesI=?', (self.lines_i,))
        db.close()
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.profile = {'format': 'cod2024_hs4x', 'source': 'COD 2024', 'sha256': self.sha}

    def reader(self):
        return DatabaseReader(self.path, profile=self.profile)

    def test_cod_requires_explicit_profile_bound_to_database_fingerprint(self):
        with self.assertRaises(DatabaseError):
            DatabaseReader(self.path)
        reader = self.reader()
        self.assertEqual(reader.source, 'COD 2024')
        self.assertIsNone(reader.metadata()['format_version'])
        self.assertEqual(reader.metadata()['reader_profile'], 'cod2024_hs4x')
        with self.assertRaises(DatabaseError):
            DatabaseReader(self.path, profile={**self.profile, 'sha256': '0' * 64})
        with self.assertRaises(DatabaseError):
            DatabaseReader(self.path, profile={**self.profile, 'format': 'unknown'})

    def test_source_search_full_peaks_and_all_binary_fields_are_preserved(self):
        reader = self.reader()
        candidates = reader.search('mineral_name', 'Synthetic mineral')
        self.assertEqual(candidates[0]['code'], '96-101-1259')
        self.assertEqual(candidates[0]['source'], 'COD 2024')
        card = reader.card('96-101-1259')
        self.assertEqual([r['intensity_percent'] for r in card['peaks']], ['100', '12.525'])
        self.assertEqual(card['database_raw']['intensity_scale'], 1000)
        self.assertEqual(base64.b64decode(card['database_raw']['lines_i_base64']), self.lines_i)
        self.assertEqual(base64.b64decode(card['database_raw']['lines_base64']), self.lines)
        self.assertIsNone(card['peaks'][0]['two_theta_deg'])
        data = original_data(card)
        prefix = 'original-data/96-101-1259/'
        self.assertEqual(data[prefix + 'LinesI.bin'], self.lines_i)
        db = sqlite3.connect(':memory:')
        db.deserialize(data[prefix + 'source-records.sqlite3'])
        self.assertEqual(db.execute('SELECT Lines,LinesI FROM general_stored').fetchone(), (self.lines, self.lines_i))
        db.close()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), self.sha)

    def test_cod_report_round_trip_and_reuse_keep_original_source(self):
        reader = self.reader()
        exporter = DatabaseExporter(self.root, reader)
        receipt = exporter.export('96-101-1259')
        self.assertEqual(exporter.export('96-101-1259'), receipt)
        card = json.loads((self.root / receipt['files']['json']['path']).read_text(encoding='utf-8'))
        self.assertEqual(card['source'], 'COD 2024')
        self.assertEqual(card['peak_count'], 2)
        original_data(card)

    def test_profile_config_is_project_relative_and_does_not_rewrite_raw_properties(self):
        (self.root / 'database.local.json').write_text(json.dumps({'path': 'cod.hsrdb', 'profile': self.profile}), encoding='utf-8')
        reader = load_reader(self.root)
        props = reader.card('96-101-1259')['database_raw']['source_records']['tables']['Properties']['rows']
        self.assertFalse(any(r['Key'] in ('Description', 'FormatVersion') for r in props))
        self.assertEqual(reader.source, 'COD 2024')

    def test_cod_download_preserves_lines_i_in_flat_download_package(self):
        reader=self.reader()
        receipt=DatabaseExporter(self.root,reader).export('96-101-1259')
        encoded=package_archive(self.root,[{'status':'success','receipt_path':receipt['receipt_path']}])
        relative,_=accept_zip(self.root,'cod-download',encoded,[{'code':'96-101-1259','source':'COD 2024'}])
        payload=download_zip(self.root,relative,[{'code':'96-101-1259','source':'COD 2024'}])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read('96-101-1259-LinesI.bin'),self.lines_i)
            self.assertTrue(all('/' not in name for name in archive.namelist()))

