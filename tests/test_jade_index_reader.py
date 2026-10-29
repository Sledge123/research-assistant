"""JADE派生索引独立格式的合成回归；不依赖私有数据库。"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from highscore_database.jade_index import (FORMAT, RAW_FORMAT, SUBSET_HEADERS, SNAPSHOT_NAMES,
    JadeIndexReader, canonical, manifest_snapshot, card_from_records, validate_index_card)
from highscore_database.jade_index_build import build_index
from highscore_database.reader import CardMissing, DatabaseError, QueryTooBroad

PROJECT = Path(__file__).resolve().parents[1]


def synthetic_source_files(*, count=1, peak_count=2, first_number=300820):
    """完全标为synthetic的81小文件，供解析、构建与跨层回归复用。"""
    payloads = {name: ('synthetic:' + name).encode('ascii') for name in SNAPSHOT_NAMES}
    for subset, header in SUBSET_HEADERS.items():
        sid = bytearray(b'J7' + struct.pack('<I', count))
        rid = bytearray(b'J7' + struct.pack('<I', count))
        entries = []
        for index in range(count):
            text = 'MnO2\x01Synthetic mineral\x01Synthetic compound'
            encoded = text.encode('gbk')
            sid_record = struct.pack('<H', len(text) + 5) + encoded + b'\0' + struct.pack('<IH', index + 1, 1)
            peak_blob = bytearray(struct.pack('<HH', 999, peak_count))
            for peak in range(peak_count):
                distance = 5.0 / (peak + 1)
                peak_blob.extend(struct.pack('<fHB', distance, 999 if peak == 0 else 55, 255) if peak == 0
                                 else struct.pack('<fHbbb', distance, 55, 1, 0, 0))
            entries.append((first_number + index, len(sid) + 1, len(rid) + 1, 0))
            sid.extend(sid_record)
            rid.extend(peak_blob)
        payloads[subset + '.sid'] = bytes(sid)
        payloads[subset + '.rid'] = bytes(rid)
        payloads[subset + '.xid'] = b'J7' + struct.pack('<HHHI', *header, 0, count) + b''.join(struct.pack('<IIII', *entry) for entry in entries)
        payloads[subset + '.zid'] = b'J7' + struct.pack('<I', count) + bytes(60) * count
        payloads[subset + '.cid'] = b'J7' + struct.pack('<I', count) + bytes(44) * count
        payloads[subset + '.lid'] = b'J7' + struct.pack('<I', count) + bytes(132) * count
    payloads['RDB-PLU2009.run'] = b'J8' + struct.pack('<I', count) + b'\0' * 74
    manifest = {'type': 'JADE_INDEX_SNAPSHOT', 'files': [
        {'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in sorted(payloads.items())]}
    payloads['snapshot-manifest.json'] = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
    return payloads


def make_card_fixture(*, peak_count=2, code='00-030-0820', subsets=('RDB-Inorganics',)):
    """给其他归档/下载测试复用的最小自洽索引卡，无需私有文件。"""
    payloads = synthetic_source_files(peak_count=peak_count, first_number=int(code.replace('-', '')))
    snapshot = manifest_snapshot(payloads['snapshot-manifest.json'])
    files = {item['name']: item for item in snapshot['files']}
    records = []
    for subset in sorted(subsets):
        record = {'subset': subset, 'index_zero_based': 0}
        for extension, start in (('xid', 12), ('sid', 6), ('rid', 6)):
            name = subset + '.' + extension
            data = payloads[name][start:]
            record[extension] = {'file_name': name, 'source_file_sha256': files[name]['sha256'],
                                 'source_file_bytes': files[name]['bytes'], 'byte_start_zero': start,
                                 'byte_end_zero_exclusive': start + len(data),
                                 'base64': base64.b64encode(data).decode('ascii')}
        records.append(record)
    return card_from_records(code, records, snapshot, source='Synthetic JADE index', database_sha256='a' * 64)


class JadeIndexTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / '.cache/tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='jade-index-', dir=base)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.root.resolve().relative_to(PROJECT.resolve())
        self.temporary.cleanup()

    def build(self, count=1):
        source = self.root / 'source'
        source.mkdir()
        for name, data in synthetic_source_files(count=count).items():
            (source / name).write_bytes(data)
        with patch('highscore_database.jade_index_build.EXPECTED_CARDS', count):
            receipt = build_index(self.root, 'source', 'derived.sqlite3', 'receipt.json')
        return receipt

    def reader(self, count=1):
        receipt = self.build(count)
        with patch('highscore_database.jade_index.EXPECTED_CARDS', count):
            reader = JadeIndexReader(self.root / receipt['path'], profile={
                'format': receipt['format'], 'source': 'Synthetic JADE index', 'sha256': receipt['sha256']})
        return reader

    def test_raw_card_round_trip_preserves_raw_intensity_and_missing_hkl(self):
        card = make_card_fixture(subsets=tuple(SUBSET_HEADERS))
        self.assertEqual(validate_index_card(card), card)
        self.assertIsNone(card['peaks'][0]['h'])
        self.assertIsNone(card['peaks'][0]['intensity_percent'])
        self.assertEqual(card['peaks'][0]['intensity_raw'], '999')
        self.assertEqual(card['peaks'][0]['intensity_scale_raw'], '999')
        self.assertFalse(card['complete_original_peak_table'])

    def test_199_peak_marker_is_exact_and_cannot_claim_original_completeness(self):
        card = make_card_fixture(peak_count=199)
        self.assertTrue(card['possible_truncation'])
        self.assertEqual(validate_index_card(card)['peak_count'], 199)
        for field, value in [('possible_truncation', False), ('complete_original_peak_table', True),
                             ('complete_index_peak_table', False), ('complete', False)]:
            changed = copy.deepcopy(card)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(DatabaseError):validate_index_card(changed)

    def test_validator_rejects_identity_peak_and_record_tampering(self):
        card = make_card_fixture()
        mutations = [lambda c: c.update(database_id='jadeidx-' + 'b' * 48),
                     lambda c: c.update(phase_name='Other'),
                     lambda c: c['peaks'][0].update(intensity_percent='100'),
                     lambda c: c['peaks'][0].update(intensity_raw='998'),
                     lambda c: c['peaks'][0].update(d_angstrom='6'),
                     lambda c: c['peaks'][0].update(two_theta_deg='30'),
                     lambda c: c.update(wavelength_angstrom='1.5406'),
                     lambda c: c['database_raw']['records'][0]['sid'].update(byte_start_zero=7),
                     lambda c: c['database_raw']['index_snapshot'].update(manifest_sha256='b' * 64)]
        for index, change in enumerate(mutations):
            changed = copy.deepcopy(card);change(changed)
            with self.subTest(index=index), self.assertRaises(DatabaseError):validate_index_card(changed)
        changed = copy.deepcopy(card)
        part = changed['database_raw']['records'][0]['rid']
        blob = bytearray(base64.b64decode(part['base64']));blob[4] ^= 1
        part['base64'] = base64.b64encode(blob).decode('ascii')
        with self.assertRaises(DatabaseError):validate_index_card(changed)

    def test_duplicate_subset_conflict_and_unknown_subset_are_rejected(self):
        card = make_card_fixture(subsets=('RDB-Inorganics', 'RDB-Minerals'))
        changed = copy.deepcopy(card)
        part = changed['database_raw']['records'][1]['sid']
        blob = bytearray(base64.b64decode(part['base64']));blob[-2] ^= 1
        part['base64'] = base64.b64encode(blob).decode('ascii')
        with self.assertRaises(DatabaseError):validate_index_card(changed)
        card['database_raw']['records'][0]['subset'] = 'Unknown'
        with self.assertRaises(DatabaseError):validate_index_card(card)

    def test_reader_exact_search_card_short_number_and_readonly_integrity(self):
        reader = self.reader()
        before = reader.path.read_bytes()
        self.assertEqual(reader.card('30-0820')['code'], '00-030-0820')
        self.assertEqual(reader.card('000300820')['peak_count'], 2)
        self.assertEqual(len(reader.search('compound_name', 'Synthetic compound')), 1)
        self.assertEqual(len(reader.search('mineral_name', 'Synthetic mineral')), 1)
        self.assertEqual(len(reader.search('formula', 'MnO2')), 1)
        self.assertEqual(reader.search('compound_name', 'synthetic compound'), [])
        self.assertEqual(reader.search('compound_name', 'Synthetic%'), [])
        self.assertEqual(reader.path.read_bytes(), before)
        with self.assertRaises(CardMissing):reader.card('99-999-9999')
        with self.assertRaises(CardMissing):reader.card('ICSD-1234')
        reader.stop_event.set()
        with self.assertRaises(DatabaseError):reader.card('00-030-0820')

    def test_reader_broad_search_fails_instead_of_truncating(self):
        reader = self.reader(101)
        with self.assertRaises(QueryTooBroad):reader.search('compound_name', 'Synthetic compound')

    def test_profile_binding_stop_and_file_changes_fail(self):
        reader = self.reader()
        with self.assertRaises(DatabaseError):JadeIndexReader(reader.path, profile={'format': FORMAT, 'source': 'Synthetic', 'sha256': 'b' * 64})
        stopped = threading.Event();stopped.set()
        with self.assertRaises(DatabaseError):JadeIndexReader(reader.path, stop_event=stopped)
        with reader.path.open('ab') as stream:stream.write(b'changed')
        with self.assertRaises(DatabaseError):reader.search('formula', 'MnO2')

    def test_builder_requires_manifest_complete_snapshot_and_never_overwrites(self):
        source = self.root / 'missing';source.mkdir()
        (source / 'snapshot-manifest.json').write_bytes(synthetic_source_files()['snapshot-manifest.json'])
        with self.assertRaises(DatabaseError):build_index(self.root, 'missing', 'bad.sqlite3', 'bad.json')
        self.assertFalse((self.root / 'bad.sqlite3').exists())
        receipt = self.build()
        before = (self.root / receipt['path']).read_bytes()
        with self.assertRaises(DatabaseError):build_index(self.root, 'source', 'derived.sqlite3', 'another.json')
        self.assertEqual((self.root / receipt['path']).read_bytes(), before)

    def test_builder_rejects_structurally_valid_source_byte_change(self):
        source = self.root / 'source';source.mkdir()
        files = synthetic_source_files()
        for name, data in files.items():(source / name).write_bytes(data)
        path = source / 'RDB-Deleted.rid'
        blob = bytearray(path.read_bytes());blob[10] ^= 1;path.write_bytes(blob)
        with self.assertRaises(DatabaseError):build_index(self.root, 'source', 'bad.sqlite3', 'bad.json')
        self.assertFalse((self.root / 'bad.sqlite3').exists())


if __name__ == '__main__':unittest.main()
