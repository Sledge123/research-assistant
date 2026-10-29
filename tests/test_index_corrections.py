"""旧索引报告受控补全：同卡原字节约束、原归档/积分保全与幂等。"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch

from highscore_database.jade_index import card_from_records, manifest_snapshot, SUBSET_HEADERS
from highscore_database.exporter import DatabaseExporter
from highscore_database import reporting_v3, reporting_v4
from highscore_downloader.archive import ArchiveError, ArchiveStore, _csv_bytes
from highscore_web.database import Database, dumps, SCHEMA
from highscore_web.credits import SCHEMA as CREDIT_SCHEMA
from highscore_web.index_corrections import register_index_correction, corrected_report
from test_jade_zid import source_with_zid
from test_jade_report_archive import FixtureReader

ROOT = Path(__file__).resolve().parents[1]


def cards():
    payloads = source_with_zid()
    snapshot = manifest_snapshot(payloads['snapshot-manifest.json'])
    sources = {item['name']: item for item in snapshot['files']}
    records = []
    for subset in ['RDB-Inorganics', 'RDB-Minerals']:
        record = {'subset': subset, 'index_zero_based': 0}
        for kind, start in [('xid', 12), ('sid', 6), ('rid', 6), ('zid', 6), ('cid', 6), ('lid', 6)]:
            name = subset + '.' + kind; raw = payloads[name][start:]
            record[kind] = {'file_name': name, 'source_file_sha256': sources[name]['sha256'],
                'source_file_bytes': sources[name]['bytes'], 'byte_start_zero': start,
                'byte_end_zero_exclusive': start + len(raw), 'base64': base64.b64encode(raw).decode()}
        records.append(record)
    old = [{k: v for k, v in r.items() if k not in ['zid', 'cid', 'lid']} for r in records]
    return (card_from_records('00-030-0820', old, snapshot, source='Synthetic JADE', database_sha256='a' * 64),
            card_from_records('00-030-0820', records, snapshot, source='Synthetic JADE', database_sha256='b' * 64))


class IndexCorrectionsTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.cache/tests'; base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='index-correction-', dir=base)
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        # 使用实际业务表定义，在一笔事务初始化所需表，避免慢盘逐DDL提交。
        path = self.root / 'output/web/state.sqlite3'; path.parent.mkdir(parents=True)
        db = sqlite3.connect(path); db.executescript('BEGIN;\n' + SCHEMA + CREDIT_SCHEMA + '\nCOMMIT;'); db.close()
        self.store = Database.__new__(Database); self.store.path = path
        self.job = 'synthetic-original'; self.admin = 'synthetic-admin'
        old, new = cards(); self.code = old['code']
        original = self.root / 'output/web/artifacts' / self.job; original.mkdir(parents=True)
        card = copy.deepcopy(old)
        card['verification'] = {'method': 'jade_index_readonly_report', 'checked_at': 'synthetic',
            'peak_count': card['peak_count'], 'database_sha256': card['database_sha256'],
            'rtf_origin': 'generated_index_report', 'native_highscore_export': False,
            'report_version': 4, 'completeness_scope': 'verified_index_records'}
        card['report_fields'] = reporting_v4.report_fields(card); card['raw_text'] = reporting_v4.report_text(card)
        native = original / 'old.rtf'; native.write_bytes(reporting_v4.report_rtf(card))
        receipt = ArchiveStore(original).save(card, native, card['code'])
        receipt.update(export_method='jade_index_readonly_report', rtf_origin='generated_index_report', database_id=card['database_id'], report_version=4)
        (original / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
        ArchiveStore(original).validate(receipt['receipt_path'])
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO users VALUES (?,?,?,1,1,0)', (self.admin, 'admin@example.invalid', 'synthetic'))
            db.execute('INSERT INTO users VALUES (?,?,?,1,0,0)', ('owner', 'owner@example.invalid', 'synthetic'))
            db.execute("INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,database_id,archive_status,artifact_dir,artifact_expected,created_at,updated_at) VALUES (?,?, 'archive','succeeded','{}','synthetic','synthetic','synthetic','success',?,?,0,0)",
                (self.job, 'owner', original.relative_to(self.root).as_posix(), dumps([{'code': self.code, 'source': old['source']}])) )
            db.execute("INSERT INTO credit_entries VALUES ('synthetic-credit','owner','xrd_charge',0,-1,'synthetic',?,0)", (self.job,))
            db.execute("INSERT INTO xrd_credit_holds VALUES (?,'owner',1,1)", (self.job,))
        self.corrected = self.root / 'corrected'; self.corrected.mkdir()
        self.new_receipt = DatabaseExporter(self.corrected, FixtureReader(new)).export(self.code)
        self.original = original; self.old_card = card; self.old_receipt = receipt

    def read(self):
        payloads = {self.code + '.' + kind: (self.original / item['path']).read_bytes() for kind, item in self.old_receipt['files'].items()}
        receipt_bytes = (self.original / self.old_receipt['receipt_path']).read_bytes()
        return corrected_report(self.root, self.job, self.old_card, payloads, receipt_bytes)

    def snapshot(self):
        with self.store.connection() as db:
            rows = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)] for table in ['jobs','credit_entries','xrd_credit_holds']}
        files = {p.relative_to(self.original).as_posix(): p.read_bytes() for p in self.original.rglob('*') if p.is_file()}
        return rows, files

    def change_original_report_version(self, version):
        model = reporting_v3 if version == 3 else reporting_v4
        self.old_card['verification']['report_version'] = version
        self.old_card['report_fields'] = model.report_fields(self.old_card)
        self.old_card['raw_text'] = model.report_text(self.old_card)
        payloads = {'json': (json.dumps(self.old_card, ensure_ascii=False, indent=2) + '\n').encode(),
                    'txt': self.old_card['raw_text'].encode(), 'rtf': model.report_rtf(self.old_card),
                    'csv': _csv_bytes(self.old_card)}
        self.old_receipt['report_version'] = version
        for kind, data in payloads.items():
            item = self.old_receipt['files'][kind]
            (self.original / item['path']).write_bytes(data)
            item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.original / self.old_receipt['receipt_path']).write_text(json.dumps(self.old_receipt), encoding='utf-8')

    def test_version3_original_uses_its_own_frozen_contract_and_dynamic_proof(self):
        self.change_original_report_version(3)
        ArchiveStore(self.original).validate(self.old_receipt['receipt_path'])
        before = self.snapshot()
        proof = register_index_correction(self.root, self.job, self.corrected, self.admin)
        self.assertEqual(proof['from_version'], 3)
        self.assertEqual(proof['to_version'], 6)
        self.assertEqual(self.read()[2], proof)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(register_index_correction(self.root, self.job, self.corrected, self.admin), proof)

    def test_unverified_version2_index_correction_is_explicitly_rejected(self):
        self.change_original_report_version(2)
        before = self.snapshot()
        with self.assertRaises(ArchiveError):
            register_index_correction(self.root, self.job, self.corrected, self.admin)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / 'output/web/report-corrections').exists())

    def test_no_correction_returns_none_and_does_not_create_directories(self):
        self.assertIsNone(self.read())
        self.assertFalse((self.root / 'output/web/report-corrections').exists())

    def test_same_source_byte_bound_correction_is_free_atomic_and_idempotent(self):
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,database_id,created_at,updated_at) VALUES ('unrelated-frozen','owner','archive','queued','{}','synthetic','unrelated','synthetic',0,0)")
            db.execute("INSERT INTO xrd_credit_holds VALUES ('unrelated-frozen','owner',2,NULL)")
            db.execute("INSERT INTO credit_entries VALUES ('unrelated-freeze','owner','xrd_freeze',-2,2,'unrelated','unrelated-frozen',0)")
        before = self.snapshot()
        first = register_index_correction(self.root, self.job, self.corrected, self.admin)
        second = register_index_correction(self.root, self.job, self.corrected, self.admin)
        self.assertEqual(first, second)
        card, payloads, proof = self.read()
        self.assertEqual(card['space_group'], 'Pmmn')
        from highscore_database.jade_index import validate_index_card
        self.assertEqual(validate_index_card(card)['peaks'], self.old_card['peaks'])
        self.assertEqual(card['verification']['report_version'], 6)
        self.assertEqual(proof['from_database_sha256'], 'a' * 64)
        self.assertEqual(proof['to_database_sha256'], 'b' * 64)
        self.assertEqual(json.loads(payloads[self.code + '.json'])['index_metadata'], card['index_metadata'])
        self.assertEqual(before, self.snapshot())
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='xrd_index_zid_correction_registered'").fetchone()[0], 1)
        self.assertEqual(self.read(), (card, payloads, proof))
        different = self.root / 'different-package'
        shutil.copytree(self.corrected, different)
        changed = copy.deepcopy(card); changed['verification']['checked_at'] = 'synthetic different package'
        path = different / self.new_receipt['files']['json']['path']
        raw = (json.dumps(changed, ensure_ascii=False, indent=2) + '\n').encode()
        path.write_bytes(raw)
        receipt = copy.deepcopy(self.new_receipt)
        receipt['files']['json'].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        (different / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
        ArchiveStore(different).validate(receipt['receipt_path'])
        with self.assertRaises(ArchiveError): register_index_correction(self.root, self.job, different, self.admin)
        self.assertEqual(self.read(), (card, payloads, proof))

    def test_requires_live_verified_admin_and_original_successful_index(self):
        for verified, admin in [(0,1),(1,0)]:
            with self.store.connection(write=True) as db: db.execute('UPDATE users SET verified=?,is_admin=? WHERE id=?', (verified,admin,self.admin))
            with self.assertRaises(ArchiveError): register_index_correction(self.root, self.job, self.corrected, self.admin)
        self.assertFalse((self.root / 'output/web/report-corrections').exists())

    def test_tampered_new_payload_or_original_binding_stops_read(self):
        register_index_correction(self.root, self.job, self.corrected, self.admin)
        new_files = list((self.root / 'output/web/report-corrections').rglob(self.code + '.txt'))
        self.assertEqual(len(new_files), 1); original = new_files[0].read_bytes()
        new_files[0].write_text('tampered', encoding='utf-8')
        with self.assertRaises(ArchiveError): self.read()
        new_files[0].write_bytes(original)
        old_receipt = self.original / self.old_receipt['receipt_path']
        old_receipt.write_bytes(old_receipt.read_bytes() + b'\n')
        with self.assertRaises(ArchiveError): self.read()

    def test_same_number_different_source_or_snapshot_is_rejected(self):
        original, changed = cards()
        changed['source'] = 'Different source'
        other = self.root / 'other'; other.mkdir()
        DatabaseExporter(other, FixtureReader(changed)).export(self.code)
        with self.assertRaises(ArchiveError): register_index_correction(self.root, self.job, other, self.admin)
        original, changed = cards()
        snapshot_bytes = base64.b64decode(changed['database_raw']['index_snapshot']['manifest_base64']) + b'\n'
        changed = card_from_records(self.code, changed['database_raw']['records'], manifest_snapshot(snapshot_bytes),
                                   source=changed['source'], database_sha256='c' * 64)
        another = self.root / 'another'; another.mkdir()
        DatabaseExporter(another, FixtureReader(changed)).export(self.code)
        with self.assertRaises(ArchiveError): register_index_correction(self.root, self.job, another, self.admin)
        self.assertFalse((self.root / 'output/web/report-corrections').exists())

    def test_staging_rename_failure_leaves_original_and_no_published_correction(self):
        before = self.snapshot()
        with patch('highscore_web.index_corrections._publish', side_effect=OSError('synthetic same-filesystem error')):
            with self.assertRaises(ArchiveError): register_index_correction(self.root, self.job, self.corrected, self.admin)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list((self.root / 'output/web/report-corrections').rglob('manifest.json')))


if __name__ == '__main__': unittest.main()
