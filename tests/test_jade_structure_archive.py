"""结构报告五格式、原字节复验、CIF 鉴权与积分回归；不发送邮件。"""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from highscore_database.exporter import DatabaseExporter
from highscore_database.executor import package_archive
from highscore_database.jade_structure import FORMAT, JadeStructureReader, structure_cif, structure_csv
from highscore_database.jade_structure_build import build_database
from highscore_downloader.archive import ArchiveError, ArchiveStore
from highscore_web.artifacts import accept_zip, public_results
from highscore_web.source_files import download_zip, original_data, selected_cif
from test_jade_structure_reader import fixture_source


ROOT = Path(__file__).resolve().parents[1]
BASE = '/highscore/api'


class JadeStructureArchiveTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='structure-report-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = fixture_source(self.root / 'source')
        path = self.root / 'structure.sqlite3'
        built = build_database(source, path)
        self.reader = JadeStructureReader(path, profile={
            'format': FORMAT, 'source': 'ICSD（JADE结构）', 'sha256': built['sha256'],
        })
        self.card = self.reader.card('ICSD-000001')
        self.producer = self.root / 'producer'; self.producer.mkdir()

    def export(self):
        return DatabaseExporter(self.producer, self.reader, report_settings={
            'wavelength_angstrom': '1.54060', 'source': '测试波长不适用于结构数据',
        }).export(self.card['code'])

    def saved_card(self, receipt):
        return json.loads((self.producer / receipt['files']['json']['path']).read_text(encoding='utf-8'))

    def replace_payload(self, receipt, kind, payload):
        receipt = copy.deepcopy(receipt)
        (self.producer / receipt['files'][kind]['path']).write_bytes(payload)
        receipt['files'][kind].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (self.producer / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf-8')
        return receipt

    def replace_json(self, receipt, card):
        return self.replace_payload(receipt, 'json',
            (json.dumps(card, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def unchecked_package(self, receipt, *, omit=()):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            names = [receipt['receipt_path'], *(value['path'] for kind, value in receipt['files'].items() if kind not in omit)]
            for name in names:
                archive.write(self.producer / name, name)
        return base64.b64encode(stream.getvalue()).decode('ascii')

    def test_structure_has_five_formats_explicit_symmetry_and_no_powder_peaks(self):
        receipt = self.export(); card = self.saved_card(receipt)
        self.assertEqual(set(receipt['files']), {'rtf', 'txt', 'json', 'csv', 'cif'})
        self.assertEqual((card['peaks'], card['peak_count']), ([], 0))
        self.assertTrue(card['complete_structure_records'])
        self.assertFalse(card['complete_original_peak_table'])
        self.assertNotIn('angular_conversion', card)
        self.assertIsNone(card.get('wavelength_angstrom'))
        self.assertIn('原子坐标', card['raw_text'])
        self.assertIn('对称操作', card['raw_text'])
        self.assertNotIn('峰表', card['raw_text'])
        self.assertNotIn('I [%]', card['raw_text'])
        self.assertEqual(card['verification']['method'], 'jade_structure_readonly_report')
        self.assertEqual(card['verification']['rtf_origin'], 'generated_structure_report')
        self.assertEqual(card['verification']['completeness_scope'], 'verified_structure_records')
        self.assertEqual((self.producer / receipt['files']['cif']['path']).read_bytes(), structure_cif(self.card).encode('utf-8'))
        self.assertEqual((self.producer / receipt['files']['csv']['path']).read_bytes(), structure_csv(self.card).encode('utf-8-sig'))
        self.assertEqual(self.export(), receipt)
        from test_archive import synthetic_card
        empty = synthetic_card(); empty.update(peaks=[], peak_count=0)
        native = self.producer / 'empty.rtf'; native.write_bytes(b'{\\rtf1 empty}')
        with self.assertRaisesRegex(ArchiveError, '峰表不能为空'):
            ArchiveStore(self.producer).save(empty, native, empty['code'])

    def test_scientific_json_changes_cannot_pass_recomputed_digests(self):
        receipt = self.export(); baseline = self.saved_card(receipt)
        mutations = (
            lambda card: card['structure']['cell'].update(a=99.0),
            lambda card: card['structure']['sites'][0].update(x=0.12345),
            lambda card: card['structure']['sites'][0].update(occupancy=0.5),
            lambda card: card['structure']['space_group'].update(number=1),
            lambda card: card['structure']['space_group']['symmetry_operations'].__setitem__(0, '-x,-y,-z'),
            lambda card: card.update(site_count=1),
            lambda card: card.update(complete_structure_records=False),
            lambda card: card.update(complete_original_peak_table=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                card = copy.deepcopy(baseline); mutate(card)
                changed = self.replace_json(receipt, card)
                with self.assertRaisesRegex(ArchiveError, '晶体结构原始记录'):
                    ArchiveStore(self.producer).validate(changed['receipt_path'])
                with self.assertRaises(ArchiveError): original_data(card)

    def test_changed_cif_csv_or_rtf_is_rejected_after_digest_update(self):
        receipt = self.export()
        for kind, extra in (('cif', b'\n# changed CIF\n'), ('csv', b'\n'), ('rtf', b' changed')):
            with self.subTest(kind=kind):
                original = (self.producer / receipt['files'][kind]['path']).read_bytes()
                changed = self.replace_payload(receipt, kind, original + extra)
                with self.assertRaises(ArchiveError): ArchiveStore(self.producer).validate(changed['receipt_path'])
                self.replace_payload(receipt, kind, original)
        missing = copy.deepcopy(receipt); del missing['files']['cif']
        (self.producer / receipt['receipt_path']).write_text(json.dumps(missing), encoding='utf-8')
        with self.assertRaisesRegex(ArchiveError, '文件类型'):
            ArchiveStore(self.producer).validate(receipt['receipt_path'])

    def test_original_structure_records_download_flat_without_changing_archive(self):
        receipt = self.export()
        encoded = package_archive(self.producer, [{'status': 'success', 'receipt_path': receipt['receipt_path']}])
        relative, receipts = accept_zip(self.root, 'structure-download', encoded, [{'code': self.card['code']}])
        result = public_results(self.root, relative, receipts)[0]
        self.assertEqual((result['archive_type'], result['archive_label']), ('structure_report', '晶体结构报告'))
        saved = (self.root / relative / 'artifact.zip').read_bytes()
        self.assertEqual(selected_cif(self.root, relative, [{'code': self.card['code']}], self.card['code']),
                         structure_cif(self.card).encode('utf-8'))
        self.assertIsNone(selected_cif(self.root, relative, [{'code': self.card['code']}], 'ICSD-999999'))
        with zipfile.ZipFile(io.BytesIO(download_zip(self.root, relative, [{'code': self.card['code']}]))) as archive:
            self.assertTrue(all('/' not in name and '\\' not in name for name in archive.namelist()))
            self.assertEqual(archive.read(self.card['code'] + '.cif'), structure_cif(self.card).encode('utf-8'))
            for kind, record in self.card['database_raw']['records'].items():
                self.assertEqual(archive.read(self.card['code'] + '-' + kind.upper() + '.bin'), base64.b64decode(record['base64']))
            self.assertEqual(archive.read(self.card['code'] + '-结构快照清单.json'),
                             base64.b64decode(self.card['database_raw']['snapshot']['manifest_base64']))
            self.assertFalse(any(name.endswith('.sqlite3') for name in archive.namelist()))
        self.assertEqual((self.root / relative / 'artifact.zip').read_bytes(), saved)

    def test_cif_route_owner_and_credit_settlement_reject_bad_then_charge_once(self):
        from fastapi.testclient import TestClient
        import test_shared_credits
        h = test_shared_credits.SharedCreditTests('runTest'); h.setUp(); self.addCleanup(h.doCleanups)
        denied = h.client.post(BASE + '/jobs/archive', json={
            'codes': self.card['code'], 'send_mail': False, 'request_key': 'structure-no-csrf',
        })
        self.assertEqual(denied.status_code, 403)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (10, 0))
        job = h.archive(self.card['code'], key='structure-credit').json(); claim = h.fx.claim().json()
        receipt = self.export(); original = (self.producer / receipt['files']['cif']['path']).read_bytes()
        bad = self.replace_payload(receipt, 'cif', original + b'\n# forged, digest updated\n')
        response = h.fx.complete(claim, result={'archive_status': 'success'}, artifact_zip_base64=self.unchecked_package(bad))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 1))
        receipt = self.replace_payload(receipt, 'cif', original)
        response = h.fx.complete(claim, result={'archive_status': 'success'}, artifact_zip_base64=self.unchecked_package(receipt))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 0))
        self.assertEqual(h.fx.complete(claim, result={'archive_status': 'success'},
                                      artifact_zip_base64=self.unchecked_package(receipt)).status_code, 409)
        status = h.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(status['credits']['charged'], 1)
        self.assertEqual(status['results'][0]['archive_label'], '晶体结构报告')
        url = BASE + '/jobs/' + job['id'] + '/cif-download?code=' + self.card['code']
        for _ in range(2):
            response = h.client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, original)
            self.assertEqual(response.headers['content-type'], 'chemical/x-cif')
            self.assertEqual(h.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 200)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 0))
        self.assertEqual(h.client.get(url.replace(self.card['code'], 'ICSD-999999')).status_code, 404)
        with TestClient(h.app) as anonymous: self.assertEqual(anonymous.get(url).status_code, 401)
        with h.app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u2','other-structure@example.test','unused',1,0,?)", (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                       (hashlib.sha256(b'structure-other-session').hexdigest(), 'u2', 'other-csrf', time.time() + 3600))
        h.client.cookies.set('highscore_session', 'structure-other-session')
        self.assertEqual(h.client.get(url).status_code, 404)
        h.client.cookies.set('highscore_session', 'synthetic-session')
        artifact = h.root / 'output/web/artifacts' / job['id']
        (artifact / receipt['files']['cif']['path']).write_bytes(original + b'changed')
        self.assertEqual(h.client.get(url).status_code, 409)
        self.assertEqual((h.wallet()['available'], h.wallet()['frozen']), (9, 0))


if __name__ == '__main__':
    unittest.main()
