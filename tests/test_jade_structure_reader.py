"""独立合成JCSD布局与setting回归；不包含数据库原样本。"""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from synthetic_structure import FIXTURE, SETTING_FIXTURES, fixture_source
ROOT = Path(__file__).resolve().parents[1]


class JadeStructureTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.find_spec('highscore_database.jade_structure')
        self.assertIsNotNone(spec, '结构读取器尚未实现')
        from highscore_database import jade_structure
        self.module = jade_structure
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache', prefix='structure-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def reader(self):
        from highscore_database.jade_structure_build import build_database
        source = fixture_source(self.root / 'source')
        target = self.root / 'structure.sqlite3'
        result = build_database(source, target)
        reader = self.module.JadeStructureReader(target, profile={'format': self.module.FORMAT, 'source': 'ICSD（JADE结构）', 'sha256': result['sha256']})
        return reader, source, result

    def test_source_coordinates_and_original_accession(self):
        reader, _, result = self.reader()
        card = reader.card('ICSD-000001')
        self.assertEqual(reader.content_type, 'structure')
        self.assertEqual(card['structure_id'], 1)
        self.assertEqual(card['site_count'], 9)
        self.assertEqual(card['peak_count'], 0)
        self.assertEqual(card['structure']['space_group']['number'], 14)
        self.assertEqual(card['structure']['space_group']['setting'], 'b-unique')
        self.assertEqual(len(card['structure']['space_group']['symmetry_operations']), 4)
        first = card['structure']['sites'][0]
        self.assertEqual((first['element'], first['charge'], first['multiplicity'], first['occupancy']), ('Si', 0, 4, 1.0))
        self.assertAlmostEqual(first['x'], .0625, places=6)
        self.assertEqual(self.module.validate_structure_card(card), card)
        self.assertEqual(result['structure_count'], 1)

    def test_exact_search_and_content_number_separation(self):
        reader, _, _ = self.reader()
        self.assertEqual([x['code'] for x in reader.search('compound_name', 'Synthetic nine-site structure')], ['ICSD-000001'])
        self.assertEqual(reader.search('compound_name', 'Synthetic nine-site'), [])
        from highscore_database.reader import CardMissing
        for code in ('00-030-0820', '30-0820', 'ICSD-999999'):
            with self.subTest(code=code), self.assertRaises(CardMissing): reader.card(code)

    def test_cif_and_csv_have_explicit_symmetry_and_sites(self):
        reader, _, _ = self.reader()
        card = reader.card('ICSD-000001')
        cif = self.module.structure_cif(card)
        self.assertIn('_space_group_symop_operation_xyz', cif)
        self.assertIn('_atom_site_fract_x', cif)
        self.assertIn('Si1 Si', cif)
        self.assertNotIn('_atom_site_B_iso', cif)
        self.assertEqual(len(self.module.structure_csv(card).splitlines()), 10)

    def test_card_tampering_rejected_without_source_files(self):
        reader, source, _ = self.reader()
        card = reader.card('ICSD-000001')
        for path in source.iterdir(): path.unlink()
        from highscore_database.reader import DatabaseError
        for field in ('occupancy', 'x', 'multiplicity', 'atomic_number'):
            bad = copy.deepcopy(card)
            bad['structure']['sites'][0][field] += 1
            with self.subTest(field=field), self.assertRaises(DatabaseError): self.module.validate_structure_card(bad)
        bad = copy.deepcopy(card)
        bad['structure']['space_group']['symmetry_operations'][0] = '-x,-y,-z'
        with self.assertRaises(DatabaseError): self.module.validate_structure_card(bad)
        self.assertEqual(self.module.validate_structure_card(card), card)

    def test_invalid_occupancy_is_preserved_but_unsupported(self):
        from highscore_database.jade_structure_build import build_database
        source = fixture_source(self.root / 'source')
        path = source / 'ICSD-All.csa'
        raw = bytearray(path.read_bytes())
        struct.pack_into('<f', raw, 1024 + 8 + 8, 1.5)
        path.write_bytes(raw)
        manifest = json.loads((source / 'manifest.json').read_text())
        item = next(x for x in manifest['files'] if x['name'] == path.name)
        item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        (source / 'manifest.json').write_text(json.dumps(manifest))
        target = self.root / 'structure.sqlite3'
        result = build_database(source, target)
        self.assertEqual(result['unsupported_count'], 1)
        reader = self.module.JadeStructureReader(target, profile={'format': self.module.FORMAT, 'source': 'test', 'sha256': result['sha256']})
        from highscore_database.reader import DatabaseError
        with self.assertRaisesRegex(DatabaseError, '占有率'): reader.card('ICSD-000001')

    def test_setting_axis_origin_and_nonstandard_cell_are_preserved(self):
        def setting(j):
            row = SETTING_FIXTURES[str(j)]
            return self.module.decode_setting(base64.b64decode(row['sgc']), base64.b64decode(row['sym']))
        first, second = setting(620), setting(622)
        self.assertEqual(first['number'], 225)
        self.assertEqual(second['number'], 225)
        self.assertEqual(len(first['symmetry_operations']), 192)
        self.assertEqual(len(second['symmetry_operations']), 192)
        self.assertNotEqual(first['symmetry_operations'], second['symmetry_operations'])
        self.assertIn('origin 1/8', second['setting'])
        self.assertIn('x,-y,-z', setting(151)['symmetry_operations'])
        uncommon = setting(19)
        self.assertEqual((uncommon['number'], uncommon['symbol']), (5, 'F2'))
        self.assertEqual(len(uncommon['symmetry_operations']), 8)
        self.assertEqual(len(setting(537)['symmetry_operations']), 36)
        from highscore_database.reader import DatabaseError
        raw = bytearray(base64.b64decode(SETTING_FIXTURES['622']['sym']))
        struct.pack_into('<h', raw, 4, 2)
        with self.assertRaises(DatabaseError):
            self.module.decode_setting(base64.b64decode(SETTING_FIXTURES['622']['sgc']), bytes(raw))

    def test_mutating_returned_symmetry_cannot_poison_validation_cache(self):
        reader, _, _ = self.reader()
        card = reader.card('ICSD-000001')
        card['structure']['space_group']['symmetry_operations'][0] = '-x,-y,-z'
        from highscore_database.reader import DatabaseError
        with self.assertRaises(DatabaseError): self.module.validate_structure_card(card)
        self.assertEqual(reader.card('ICSD-000001')['structure']['space_group']['symmetry_operations'][0], 'x,y,z')

    def test_wrong_offsets_and_source_hash_fail_closed(self):
        from highscore_database.jade_structure_build import build_database
        source = fixture_source(self.root / 'source')
        path = source / 'ICSD-All.csd'
        path.write_bytes(path.read_bytes() + b'X')
        from highscore_database.reader import DatabaseError
        with self.assertRaises(DatabaseError): build_database(source, self.root / 'bad.sqlite3')

    def test_actual_loaded_blob_is_hashed_and_source_changes_are_rejected(self):
        from highscore_database.jade_structure_build import build_database
        from highscore_database.reader import DatabaseError
        source = fixture_source(self.root / 'source')
        original_read = Path.read_bytes
        def changed_blob(path):
            data = original_read(path)
            if path == source / 'ICSD-All.csa':
                data = bytearray(data)
                struct.pack_into('<f', data, 1024 + 8 + 32, .25)
                return bytes(data)
            return data
        with patch.object(Path, 'read_bytes', changed_blob), self.assertRaises(DatabaseError):
            build_database(source, self.root / 'bad-loaded.sqlite3')
        self.assertFalse((self.root / 'bad-loaded.sqlite3').exists())
        from highscore_database import jade_structure_build
        original_decode = jade_structure_build.decode_payload
        def mutate_source_after_loading(*args):
            value = original_decode(*args)
            path = source / 'ICSD-All.csa'
            raw = bytearray(original_read(path))
            struct.pack_into('<f', raw, 1024 + 8 + 32, .25)
            path.write_bytes(raw)
            return value
        with patch.object(jade_structure_build, 'decode_payload', mutate_source_after_loading), self.assertRaisesRegex(DatabaseError, '变化'):
            build_database(source, self.root / 'bad-raced.sqlite3')
        self.assertFalse((self.root / 'bad-raced.sqlite3').exists())


if __name__ == '__main__': unittest.main()
