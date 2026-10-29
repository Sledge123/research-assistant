"""数据库报告不能通过删除或伪造核验类型/版本退回原生归档分支。"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from highscore_database import reporting, reporting_v3
from highscore_database.exporter import DatabaseExporter
from highscore_database.reader import DatabaseReader
from highscore_downloader.archive import ArchiveError, ArchiveStore
from test_archive import synthetic_card
from test_database_reader import fixture
from test_jade_index_reader import make_card_fixture


ROOT = Path(__file__).resolve().parents[1]


class ReportVerificationContractTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.cache/tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='report-contract-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        database = self.root / 'synthetic.hsrdb'
        fixture(database, count=1)
        self.reader = DatabaseReader(database)

    def archive_card(self, card, suffix, payload=None):
        root = self.root / suffix
        root.mkdir()
        rtf = root / 'synthetic.rtf'
        rtf.write_bytes(payload or b'{\\rtf1 synthetic native}')
        return ArchiveStore(root).save(card, rtf, card['code'])

    def database_card(self, version=5):
        card = self.reader.card('09-999-0001')
        card['verification'] = {
            'method': 'sqlite_readonly_database_report', 'report_version': version,
            'rtf_origin': 'generated_database_report', 'native_highscore_export': False,
            'completeness_scope': 'available_database_fields',
            'database_sha256': card['database_sha256'],
        }
        renderer = reporting if version >= 4 else reporting_v3
        card['report_fields'] = renderer.report_fields(card)
        card['raw_text'] = renderer.report_text(card)
        return card, renderer.report_rtf(card)

    def test_historical_versions_do_not_require_a_new_physical_snapshot(self):
        for version in (2, 3):
            with self.subTest(version=version):
                card, payload = self.database_card(version)
                card['database_raw'].pop('source_records')
                receipt = self.archive_card(card, 'legacy-' + str(version), payload)
                self.assertEqual(receipt['status'], 'success')

    def test_native_archives_with_and_without_known_native_verification_still_work(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                card = synthetic_card()
                if verified:
                    card['verification'] = {'method': 'native_detail_and_rtf'}
                self.assertEqual(self.archive_card(card, 'native-' + str(verified))['status'], 'success')

    def test_current_and_frozen_v4_reports_are_valid_but_still_reconstruct_source(self):
        for version in (4, 5):
            with self.subTest(version=version):
                card, payload = self.database_card(version)
                self.assertEqual(self.archive_card(card, 'valid-' + str(version), payload)['status'], 'success')
                card['cell_parameters']['a'] = '999'
                with self.assertRaises(ArchiveError):
                    self.archive_card(card, 'bad-science-' + str(version), payload)

    def test_database_markers_require_recognized_verification_method_and_version(self):
        mutations = (
            ('delete_verification', lambda card: card.pop('verification')),
            ('delete_method', lambda card: card['verification'].pop('method')),
            ('change_method', lambda card: card['verification'].update(method='unverified')),
            ('masquerade_native', lambda card: card['verification'].update(method='native_detail_and_rtf')),
            ('delete_version', lambda card: card['verification'].pop('report_version')),
            ('unknown_version', lambda card: card['verification'].update(report_version=999)),
            ('version_string', lambda card: card['verification'].update(report_version='4')),
            ('version_float', lambda card: card['verification'].update(report_version=4.0)),
            ('version_bool', lambda card: card['verification'].update(report_version=True)),
        )
        for name, mutate in mutations:
            with self.subTest(mutation=name):
                card, payload = self.database_card()
                mutate(card)
                with self.assertRaises(ArchiveError):
                    self.archive_card(card, name, payload)

    def test_recomputed_json_digest_cannot_hide_unknown_or_deleted_contract(self):
        root = self.root / 'saved'
        root.mkdir()
        receipt = DatabaseExporter(root, self.reader).export('09-999-0001')
        path = root / receipt['files']['json']['path']
        original = json.loads(path.read_text(encoding='utf-8'))
        mutations = (
            lambda card: card['verification'].update(report_version=999),
            lambda card: card.pop('verification'),
            lambda card: card['verification'].update(method='native_detail_and_rtf'),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                card = copy.deepcopy(original)
                mutate(card)
                card['cell_parameters']['a'] = '999'
                payload = (json.dumps(card, ensure_ascii=False) + '\n').encode('utf-8')
                path.write_bytes(payload)
                changed = copy.deepcopy(receipt)
                changed['files']['json'].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                (root / receipt['receipt_path']).write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(ArchiveError):
                    ArchiveStore(root).validate(receipt['receipt_path'])

    def test_explicit_receipt_contract_must_match_card(self):
        for key, value in (('report_version', 999), ('report_version', 3),
                           ('export_method', 'native_detail_and_rtf'), ('export_method', 'unverified')):
            with self.subTest(key=key, value=value):
                root = self.root / ('receipt-' + key + '-' + str(value))
                root.mkdir()
                receipt = DatabaseExporter(root, self.reader).export('09-999-0001')
                receipt[key] = value
                (root / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
                with self.assertRaises(ArchiveError):
                    ArchiveStore(root).validate(receipt['receipt_path'])

    def test_index_contract_rejects_unknown_or_missing_version(self):
        for version in (999, None):
            with self.subTest(version=version):
                card = make_card_fixture()
                card['verification'] = {
                    'method': reporting.INDEX_REPORT_METHOD,
                    'rtf_origin': reporting.INDEX_REPORT_ORIGIN,
                    'native_highscore_export': False,
                    'completeness_scope': 'verified_index_records',
                }
                if version is not None:
                    card['verification']['report_version'] = version
                card['report_fields'] = reporting.report_fields(card)
                card['raw_text'] = reporting.report_text(card)
                with self.assertRaises(ArchiveError):
                    self.archive_card(card, 'index-' + str(version), reporting.report_rtf(card))


if __name__ == '__main__':
    unittest.main()
