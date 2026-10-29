"""索引报告：原字节复验、原始强度、下载布局及服务器结算。"""
import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from highscore_database.exporter import DatabaseExporter
from highscore_database.executor import package_archive
from highscore_downloader.archive import ArchiveError, ArchiveStore, INDEX_PEAK_FIELDS
from highscore_web.artifacts import accept_zip, public_results
from highscore_web.source_files import download_zip, original_data, native_file
from test_jade_index_reader import make_card_fixture


ROOT = Path(__file__).resolve().parents[1]


class FixtureReader:
    def __init__(self, card):
        self.value = card

    def card(self, code):
        if code != self.value['code']:
            raise ValueError('合成卡号不匹配')
        return copy.deepcopy(self.value)


class JadeReportArchiveTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='jade-report-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.card = make_card_fixture()
        self.producer = self.root / 'producer'
        self.producer.mkdir()

    def export(self, card=None):
        return DatabaseExporter(self.producer, FixtureReader(card or self.card), report_settings={
            'wavelength_angstrom': '1.54060', 'source': '合成测试波长',
        }).export((card or self.card)['code'])

    def saved_card(self, receipt):
        return json.loads((self.producer / receipt['files']['json']['path']).read_text(encoding='utf-8'))

    def replace_json(self, receipt, card):
        path = self.producer / receipt['files']['json']['path']
        payload = (json.dumps(card, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        path.write_bytes(payload)
        receipt = copy.deepcopy(receipt)
        receipt['files']['json'].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (self.producer / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
        return receipt

    def unchecked_package(self, receipt):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in [receipt['receipt_path'], *(value['path'] for value in receipt['files'].values())]:
                archive.write(self.producer / name, name)
        return base64.b64encode(stream.getvalue()).decode('ascii')

    def test_report_and_csv_preserve_raw_values_and_index_scope(self):
        receipt = self.export()
        card = self.saved_card(receipt)
        for current, original in zip(card['peaks'], self.card['peaks']):
            self.assertEqual({k:v for k,v in current.items() if k not in ('two_theta_deg','intensity_percent')},
                             {k:v for k,v in original.items() if k not in ('two_theta_deg','intensity_percent')})
        self.assertEqual(card['database_raw'], self.card['database_raw'])
        self.assertTrue(card['complete_index_peak_table'])
        self.assertFalse(card['complete_original_peak_table'])
        self.assertEqual(card['wavelength_angstrom'], '1.54060')
        self.assertEqual(card['angular_conversion']['two_theta_origin'], 'calculated')
        self.assertIn('I [%]', card['raw_text'])
        self.assertIn('2θ [°]', card['raw_text'])
        self.assertEqual(card['intensity_normalization']['method'], 'index_table_maximum')
        self.assertEqual(card['verification']['method'], 'jade_index_readonly_report')
        self.assertEqual(card['verification']['rtf_origin'], 'generated_index_report')
        self.assertEqual(card['verification']['completeness_scope'], 'verified_index_records')
        self.assertEqual(receipt['export_method'], 'jade_index_readonly_report')
        csv_text = (self.producer / receipt['files']['csv']['path']).read_text(encoding='utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(tuple(rows[0]), INDEX_PEAK_FIELDS)
        self.assertEqual(len(rows), self.card['peak_count'])
        for row, peak in zip(rows, self.card['peaks']):
            self.assertEqual(row['d_angstrom'], peak['d_angstrom'])
            self.assertEqual(row['intensity_raw'], peak['intensity_raw'])
            self.assertEqual(row['intensity_scale_raw'], peak['intensity_scale_raw'])
            self.assertIsNotNone(row['two_theta_deg'])
            self.assertAlmostEqual(float(row['intensity_percent']), 100*int(peak['intensity_raw'])/999, places=5)
        self.assertEqual(self.export(), receipt)

    def test_199_peaks_keep_possible_truncation_without_claiming_original_completeness(self):
        card = make_card_fixture(peak_count=199)
        saved = self.saved_card(self.export(card))
        self.assertEqual(len(saved['peaks']), 199)
        self.assertTrue(saved['possible_truncation'])
        self.assertTrue(saved['complete_index_peak_table'])
        self.assertFalse(saved['complete_original_peak_table'])

    def test_server_reparses_bytes_after_json_digest_is_recomputed(self):
        receipt = self.export()
        baseline = self.saved_card(receipt)
        mutations = (
            lambda card: card['peaks'][0].update(d_angstrom='99.0'),
            lambda card: card['peaks'][0].update(intensity_raw='17'),
            lambda card: card['peaks'][0].update(intensity_percent='99'),
            lambda card: card['peaks'][0].update(two_theta_deg='99'),
            lambda card: card['intensity_normalization'].update(maximum_intensity_raw='1000'),
            lambda card: card.update(complete_original_peak_table=True),
            lambda card: card.update(possible_truncation=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                card = copy.deepcopy(baseline)
                mutate(card)
                changed_receipt = self.replace_json(receipt, card)
                with self.assertRaisesRegex(ArchiveError, '索引原始记录'):
                    ArchiveStore(self.producer).validate(changed_receipt['receipt_path'])
                with self.assertRaises(ArchiveError):
                    original_data(card)

    def test_changed_rtf_is_rejected_even_with_recomputed_digest(self):
        receipt = self.export()
        path = self.producer / receipt['files']['rtf']['path']
        payload = b'{\\rtf1 a different generated report}'
        path.write_bytes(payload)
        receipt['files']['rtf'].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (self.producer / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
        with self.assertRaisesRegex(ArchiveError, 'RTF'):
            ArchiveStore(self.producer).validate(receipt['receipt_path'])

    def test_original_download_preserves_every_subset_and_is_flat(self):
        card = make_card_fixture(subsets=('RDB-Inorganics', 'RDB-Minerals'))
        receipt = self.export(card)
        encoded = package_archive(self.producer, [{'status': 'success', 'receipt_path': receipt['receipt_path']}])
        relative, receipts = accept_zip(self.root, 'index-job', encoded, [{'code': card['code'], 'source': card['source']}])
        result = public_results(self.root, relative, receipts)[0]
        self.assertEqual((result['archive_type'], result['archive_label']), ('index_report', '索引数据报告'))
        saved_zip = (self.root / relative / 'artifact.zip').read_bytes()
        content = download_zip(self.root, relative, [{'code': card['code']}])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertTrue(all('/' not in name and '\\' not in name for name in archive.namelist()))
            self.assertEqual(archive.read(card['code'] + '-索引快照清单.json'),
                             base64.b64decode(card['database_raw']['index_snapshot']['manifest_base64']))
            for record in card['database_raw']['records']:
                for kind in ('xid', 'sid', 'rid'):
                    name = card['code'] + '-' + record['subset'] + '-' + kind.upper() + '.bin'
                    self.assertEqual(archive.read(name), base64.b64decode(record[kind]['base64']))
            self.assertFalse(any(name.endswith('.sqlite3') or 'HighScore-original' in name for name in archive.namelist()))
        self.assertEqual((self.root / relative / 'artifact.zip').read_bytes(), saved_zip)
        self.assertIsNone(native_file(self.root, card))

    def test_server_rejects_bad_index_before_charging_then_accepts_real_index_once(self):
        import test_shared_credits
        h = test_shared_credits.SharedCreditTests('runTest')
        h.setUp()
        self.addCleanup(h.doCleanups)
        job = h.archive(self.card['code'], key='index-credit').json()
        claim = h.fx.claim().json()
        receipt = self.export()
        original_card = self.saved_card(receipt)
        changed = copy.deepcopy(original_card)
        changed['peaks'][0]['d_angstrom'] = '99.0'
        bad_receipt = self.replace_json(receipt, changed)
        response = h.fx.complete(claim, result={'archive_status': 'success'},
                                 artifact_zip_base64=self.unchecked_package(bad_receipt))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 1))
        receipt = self.replace_json(receipt, original_card)
        encoded = self.unchecked_package(receipt)
        response = h.fx.complete(claim, result={'archive_status': 'success'}, artifact_zip_base64=encoded)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 0))
        status = h.client.get('/highscore/api/jobs/' + job['id']).json()
        self.assertEqual(status['credits']['charged'], 1)
        self.assertEqual(status['results'][0]['archive_label'], '索引数据报告')
        for _ in range(2):
            self.assertEqual(h.client.get('/highscore/api/jobs/' + job['id'] + '/download').status_code, 200)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 0))


if __name__ == '__main__':
    unittest.main()
