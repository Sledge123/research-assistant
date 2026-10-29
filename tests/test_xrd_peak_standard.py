"""实际归档及下载接缝上的峰表回归：角度、百分数和冻结历史契约。"""
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile

from highscore_database import reporting, reporting_v5
from highscore_database.exporter import DatabaseExporter, load_report_settings, prepare_card
from highscore_database.executor import package_archive
from highscore_database.reader import DatabaseError
from highscore_downloader.archive import ArchiveError, ArchiveStore
from highscore_web.artifacts import accept_zip
from highscore_web.source_files import download_zip
from test_jade_index_reader import make_card_fixture
from test_jade_report_archive import FixtureReader

ROOT = Path(__file__).resolve().parents[1]


class XrdPeakStandardTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='peak-standard-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def export(self, count=2):
        source = make_card_fixture(peak_count=count)
        producer = self.root / ('producer-' + str(count))
        producer.mkdir()
        receipt = DatabaseExporter(producer, FixtureReader(source)).export(source['code'])
        card = json.loads((producer / receipt['files']['json']['path']).read_text(encoding='utf-8'))
        return producer, receipt, card, source

    def test_index_uses_explicit_default_wavelength_and_normalized_intensity(self):
        producer, receipt, card, original = self.export()
        self.assertEqual(card['verification']['report_version'], 6)
        self.assertEqual(card['database_raw'], original['database_raw'])
        self.assertEqual(card['wavelength_angstrom'], '1.54060')
        self.assertEqual(max(float(p['intensity_percent']) for p in card['peaks']), 100)
        for peak, raw in zip(card['peaks'], original['peaks']):
            self.assertAlmostEqual(float(peak['two_theta_deg']), math.degrees(2*math.asin(1.5406/(2*float(raw['d_angstrom'])))), places=3)
            self.assertAlmostEqual(float(peak['intensity_percent']), 100*int(raw['intensity_raw'])/999, places=5)
        self.assertIn('2θ [°]', card['raw_text'])
        self.assertIn('I [%]', card['raw_text'])
        ArchiveStore(producer).validate(receipt['receipt_path'])

    def test_full_199_index_keeps_unreachable_peaks_and_truncation_flag(self):
        producer, receipt, card, original = self.export(count=199)
        self.assertEqual(card['peak_count'], 199)
        self.assertTrue(card['possible_truncation'])
        self.assertFalse(card['complete_original_peak_table'])
        for peak in card['peaks']:
            self.assertIsNotNone(peak['intensity_percent'])
            if 1.5406 > 2*float(peak['d_angstrom']):
                self.assertIsNone(peak['two_theta_deg'])
            else:
                self.assertIsNotNone(peak['two_theta_deg'])
        self.assertIn('无解', card['raw_text'])
        self.assertEqual(card['database_raw'], original['database_raw'])

    def test_index_normalization_uses_observed_maximum_not_unknown_header_scale(self):
        source = make_card_fixture()
        source['peaks'][0]['intensity_raw'] = '500'
        source['peaks'][1]['intensity_raw'] = '125'
        # 单独验证确定的数学规则；真实导出仍必须从原字节独立复验。
        result = prepare_card(source, load_report_settings(self.root))
        self.assertEqual([p['intensity_percent'] for p in result['peaks']], ['100', '25'])
        self.assertEqual(result['peaks'][0]['intensity_scale_raw'], '999')
        self.assertFalse(result['intensity_normalization']['original_header_scale_used'])

    def test_no_positive_intensity_is_explicit_failure(self):
        source = make_card_fixture()
        for peak in source['peaks']:
            peak['intensity_raw'] = '0'
        with self.assertRaisesRegex(DatabaseError, '正强度'):
            prepare_card(source, load_report_settings(self.root))

    def test_new_contract_rejects_deleted_or_forged_derivation(self):
        producer, receipt, card, _ = self.export()
        from highscore_downloader.archive import _check_card
        for mutate in (lambda c: c.pop('angular_conversion'),
                       lambda c: c['peaks'][1].update(two_theta_deg='1'),
                       lambda c: c['peaks'][1].update(intensity_percent='100'),
                       lambda c: c['intensity_normalization'].update(maximum_intensity_raw='55'),
                       lambda c: c['angular_conversion'].update(wavelength_angstrom='0.5')):
            changed = copy.deepcopy(card)
            mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(ArchiveError):
                _check_card(changed)

    def test_old_v5_index_download_upgrades_without_overwriting_original(self):
        card = make_card_fixture()
        card['verification'] = {'method': reporting.INDEX_REPORT_METHOD, 'report_version': 5,
            'rtf_origin': reporting.INDEX_REPORT_ORIGIN, 'native_highscore_export': False,
            'database_sha256': card['database_sha256'], 'completeness_scope': 'verified_index_records'}
        card['report_fields'] = reporting_v5.report_fields(card)
        card['raw_text'] = reporting_v5.report_text(card)
        producer = self.root / 'old-v5'; producer.mkdir()
        rtf = producer / 'old.rtf'; rtf.write_bytes(reporting_v5.report_rtf(card))
        receipt = ArchiveStore(producer).save(card, rtf, card['code'])
        encoded = package_archive(producer, [{'status': 'success', 'receipt_path': receipt['receipt_path']}])
        expected = [{'code': card['code'], 'source': card['source']}]
        relative, _ = accept_zip(self.root, 'legacy-v5', encoded, expected)
        before = {p.relative_to(self.root / relative).as_posix(): p.read_bytes() for p in (self.root / relative).rglob('*') if p.is_file()}
        for _ in range(2):
            upgrades = []
            content = download_zip(self.root, relative, expected, upgrades=upgrades)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                updated = json.loads(archive.read(card['code'] + '.json'))
                self.assertEqual(updated['verification']['report_version'], 6)
                self.assertEqual(updated['database_raw'], card['database_raw'])
                self.assertTrue(all(p['two_theta_deg'] and p['intensity_percent'] for p in updated['peaks']))
                self.assertIn('2θ [°]', archive.read(card['code'] + '.txt').decode())
            self.assertEqual((upgrades[0]['from_version'], upgrades[0]['to_version']), (5, 6))
        after = {p.relative_to(self.root / relative).as_posix(): p.read_bytes() for p in (self.root / relative).rglob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
