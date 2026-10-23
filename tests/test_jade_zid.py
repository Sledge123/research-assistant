"""ZID原字节必须被独立派生、保留并在服务器重解析。"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from highscore_database.reader import DatabaseError
from test_jade_index_reader import synthetic_source_files, make_card_fixture

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_ZID = struct.pack('<8B11f8s', 0, 3, 59, 0, 77, 4, 4, 0, 2., 4.25, 2.5, 200., 17., 12.5, 5., 4., 90., 90., 90., b'Pmmn    ')


def source_with_zid(count=1):
    from highscore_database.jade_index import SUBSET_HEADERS
    source = synthetic_source_files(count=count)
    for subset in SUBSET_HEADERS:
        source[subset + '.zid'] = b'J7' + struct.pack('<I', count) + SYNTHETIC_ZID * count
    manifest = {'type': 'JADE_INDEX_SNAPSHOT', 'files': [
        {'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        for name, raw in sorted(source.items()) if name != 'snapshot-manifest.json']}
    source['snapshot-manifest.json'] = json.dumps(manifest).encode()
    return source


class JadeZidTests(unittest.TestCase):
    def test_synthetic_sixty_byte_layout_restores_metadata_without_borrowing(self):
        from highscore_database.jade_index import decode_zid
        value = decode_zid(SYNTHETIC_ZID)
        self.assertEqual(value['space_group'], 'Pmmn')
        self.assertEqual(value['space_group_number'], 59)
        self.assertEqual(value['crystal_system_code'], 'O')
        self.assertEqual(value['z'], '2.0')
        self.assertAlmostEqual(float(value['cell_parameters']['a']), 12.5, places=5)
        self.assertAlmostEqual(float(value['density_raw']), 4.25, places=5)
        self.assertAlmostEqual(float(value['rir']), 2.5, places=5)
        self.assertEqual(value['uninterpreted_float32_offset24'], '17.0')
        self.assertEqual(value['uninterpreted_prefix_hex'], SYNTHETIC_ZID[:8].hex())

    def test_missing_values_unknown_codes_and_setting_stay_distinct(self):
        from highscore_database.jade_index import decode_zid
        value = decode_zid(bytes(60))
        self.assertIsNone(value['space_group'])
        self.assertIsNone(value['space_group_number'])
        self.assertIsNone(value['density_raw'])
        self.assertTrue(all(v is None for v in value['cell_parameters'].values()))
        changed = bytearray(SYNTHETIC_ZID)
        changed[1] = 6
        changed[3] = 2
        changed[0] = 4
        value = decode_zid(bytes(changed))
        self.assertEqual(value['crystal_system_raw_u8'], 6)
        self.assertIsNone(value['crystal_system_code'])
        self.assertEqual(value['setting_raw_u8'], 2)
        self.assertEqual(value['quality_raw_u8'], 4)

    def test_trigonal_and_hexagonal_raw_code_is_resolved_only_by_original_group_number(self):
        from highscore_database.jade_index import decode_zid
        for number, expected in ((166, 'R'), (194, 'H'), (0, None), (59, None)):
            changed = bytearray(SYNTHETIC_ZID)
            changed[1] = 5
            changed[2] = number
            self.assertEqual(decode_zid(bytes(changed))['crystal_system_code'], expected)

    def test_nonfinite_and_invalid_record_size_fail(self):
        from highscore_database.jade_index import decode_zid
        for raw in (b'x', SYNTHETIC_ZID[:-1], SYNTHETIC_ZID + b'\0',
                    SYNTHETIC_ZID[:28] + struct.pack('<f', float('nan')) + SYNTHETIC_ZID[32:]):
            with self.subTest(raw=raw.hex()), self.assertRaises(DatabaseError):
                decode_zid(raw)

    def test_new_builder_binds_zid_and_browse_uses_it_without_reading_full_card(self):
        from highscore_database.jade_index import JadeIndexReader, FORMAT_V2, RAW_FORMAT_V2, validate_index_card
        from highscore_database.jade_index_build import build_index
        base = ROOT / '.cache/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='zid-', dir=base) as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name, data in source_with_zid().items():
                (source / name).write_bytes(data)
            with patch('highscore_database.jade_index_build.EXPECTED_CARDS', 1):
                receipt = build_index(root, 'source', 'v2.sqlite3', 'v2.json')
            self.assertEqual(receipt['format'], FORMAT_V2)
            with patch('highscore_database.jade_index.EXPECTED_CARDS', 1):
                reader = JadeIndexReader(root / 'v2.sqlite3', profile={
                    'format': FORMAT_V2, 'source': 'Synthetic source', 'sha256': receipt['sha256']})
            card = reader.card('00-030-0820')
            self.assertEqual(card['database_raw']['format'], RAW_FORMAT_V2)
            self.assertEqual(card['space_group'], 'Pmmn')
            self.assertEqual(card['index_metadata']['space_group_number'], 59)
            self.assertAlmostEqual(float(card['cell_parameters']['a']), 12.5, places=5)
            for record in card['database_raw']['records']:
                self.assertEqual(set(record), {'subset', 'index_zero_based', 'xid', 'sid', 'rid', 'zid', 'cid', 'lid'})
                self.assertEqual(base64.b64decode(record['zid']['base64']), SYNTHETIC_ZID)
                self.assertEqual(record['zid']['byte_start_zero'], 6)
                self.assertEqual(len(base64.b64decode(record['cid']['base64'])), 44)
                self.assertEqual(len(base64.b64decode(record['lid']['base64'])), 132)
            self.assertEqual(validate_index_card(card), card)
            with patch.object(reader, 'card', side_effect=AssertionError('候选不能读取完整峰表')):
                candidate = reader.browse('codes', '30-0820')[0]
            self.assertEqual(candidate['space_group'], 'Pmmn')
            self.assertEqual(candidate['cell_parameters'], card['cell_parameters'])
            changed = copy.deepcopy(card)
            changed['index_metadata']['rir'] = '999'
            with self.assertRaises(DatabaseError):
                validate_index_card(changed)
            changed = copy.deepcopy(card)
            part = changed['database_raw']['records'][-1]['lid']
            raw = bytearray(base64.b64decode(part['base64']))
            raw[2] = 1
            raw[3] = 1
            part['base64'] = base64.b64encode(raw).decode()
            self.assertEqual(validate_index_card(changed), changed)
            raw[4] = 1
            part['base64'] = base64.b64encode(raw).decode()
            with self.assertRaises(DatabaseError):
                validate_index_card(changed)
            changed = copy.deepcopy(card)
            part = changed['database_raw']['records'][-1]['cid']
            part['base64'] = base64.b64encode(bytes(43) + b'\1').decode()
            with self.assertRaises(DatabaseError):
                validate_index_card(changed)
            changed = copy.deepcopy(card)
            changed['database_raw']['records'][0]['zid']['byte_start_zero'] += 1
            with self.assertRaises(DatabaseError):
                validate_index_card(changed)

    def test_existing_v1_raw_cards_keep_their_original_shape(self):
        from highscore_database.jade_index import FORMAT, RAW_FORMAT, validate_index_card
        card = make_card_fixture()
        self.assertEqual(card['database_raw']['reader_profile'], FORMAT)
        self.assertEqual(card['database_raw']['format'], RAW_FORMAT)
        self.assertNotIn('index_metadata', card)
        self.assertEqual(validate_index_card(card), card)

    def test_existing_v1_database_profile_is_still_readonly_and_does_not_invent_zid(self):
        from highscore_database.jade_index import FORMAT, JadeIndexReader
        from highscore_database.jade_index_build import build_index
        base = ROOT / '.cache/tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='zid-legacy-', dir=base) as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name, data in source_with_zid().items():
                (source / name).write_bytes(data)
            with patch('highscore_database.jade_index_build.EXPECTED_CARDS', 1):
                receipt = build_index(root, 'source', 'legacy.sqlite3', 'legacy.json', format_version=1)
            database = root / 'legacy.sqlite3'
            before = database.read_bytes()
            with patch('highscore_database.jade_index.EXPECTED_CARDS', 1):
                reader = JadeIndexReader(database, profile={
                    'format': FORMAT, 'source': 'Synthetic legacy', 'sha256': receipt['sha256']})
            card = reader.card('00-030-0820')
            self.assertIsNone(card['space_group'])
            self.assertNotIn('index_metadata', card)
            self.assertEqual(set(card['database_raw']['records'][0]),
                             {'subset', 'index_zero_based', 'xid', 'sid', 'rid'})
            self.assertEqual(database.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
