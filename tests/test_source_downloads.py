"""原始记录与原生文件：字节保真、身份绑定、损坏拒绝和账号隔离。"""
import base64
import hashlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
import zipfile

from highscore_database.reader import DatabaseReader
from highscore_database.exporter import DatabaseExporter
from highscore_database.executor import package_archive
from highscore_downloader.archive import ArchiveError
from highscore_web.artifacts import accept_zip
from highscore_web.source_files import download_zip,native_file,original_data
from test_database_reader import fixture

ROOT=Path(__file__).resolve().parents[1]


def register_synthetic(root,card):
    path=root/'output/web/native-cards'/card['database_id']/card['code'];path.mkdir(parents=True)
    payload=b'{\\rtf1 synthetic native file; unchanged bytes}'
    (path/(card['code']+'.rtf')).write_bytes(payload)
    core={key:card[key] for key in ('code','phase_name','chemical_formula','space_group')}
    core['peaks']=[{**row,'d_angstrom':format(float(row['d_angstrom']),'.5f')} for row in card['peaks']]
    manifest={'kind':'highscore_native_export','database_id':card['database_id'],'code':card['code'],
              'database_sha256':card['database_sha256'],'rtf_sha256':hashlib.sha256(payload).hexdigest(),
              'wavelength_angstrom':None,'native_core':core}
    (path/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    return path,payload


class SourceDownloadTests(unittest.TestCase):
    def setUp(self):
        cache=ROOT/'.cache/tests';cache.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='source-',dir=cache);self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);database=self.root/'synthetic.hsrdb';fixture(database)
        self.reader=DatabaseReader(database);self.card=self.reader.card('09-999-0001')

    def test_original_binary_fields_are_byte_exact_and_metadata_is_not_report_text(self):
        data=original_data(self.card);prefix='original-data/09-999-0001/'
        self.assertEqual(data[prefix+'Lines.bin'],base64.b64decode(self.card['database_raw']['lines_base64']))
        self.assertEqual(data[prefix+'HKL.bin'],base64.b64decode(self.card['database_raw']['hkl_base64']))
        record=json.loads(data[prefix+'record.json'])
        self.assertEqual(record['general_text_content'],self.card['database_raw']['text'])
        self.assertEqual(record['general_stored']['Comment'],self.card['comments'])
        self.assertFalse(record['independent_rtf_in_database'])
        self.assertNotIn('raw_text',record)
        snapshot=sqlite3.connect(':memory:')
        try:
            snapshot.deserialize(data[prefix+'source-records.sqlite3'])
            self.assertEqual(snapshot.execute('SELECT Lines,HKL FROM general_stored').fetchone(),
                             (data[prefix+'Lines.bin'],data[prefix+'HKL.bin']))
            self.assertEqual(snapshot.execute('SELECT c5color FROM general_text_content').fetchone()[0],'Blue')
            self.assertEqual(snapshot.execute('SELECT typeof(id),typeof(A) FROM general_indexed').fetchone(),('integer','real'))
            self.assertEqual(snapshot.execute('SELECT count(*) FROM general_indexed').fetchone()[0],1)
        finally:snapshot.close()

    def test_reordered_or_changed_binary_data_cannot_pass_as_original_peaks(self):
        self.card['database_raw']['lines_base64']=base64.b64encode(b'bad').decode()
        with self.assertRaises(ArchiveError):original_data(self.card)

    def test_native_bytes_are_preserved_and_corruption_or_identity_change_is_rejected(self):
        path,payload=register_synthetic(self.root,self.card)
        self.assertEqual(native_file(self.root,self.card),payload)
        manifest=json.loads((path/'manifest.json').read_text())
        manifest['native_core']['peaks'][0]['h']='99'
        (path/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ArchiveError):native_file(self.root,self.card)
        manifest['native_core']['peaks'][0]['h']=self.card['peaks'][0]['h']
        (path/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        (path/(self.card['code']+'.rtf')).write_bytes(payload+b'changed')
        with self.assertRaises(ArchiveError):native_file(self.root,self.card)

    def test_single_card_download_is_flat_complete_and_preserves_saved_archive(self):
        producer=self.root/'producer';producer.mkdir()
        receipt=DatabaseExporter(producer,self.reader).export(self.card['code'])
        encoded=package_archive(producer,[{'status':'success','receipt_path':receipt['receipt_path']}])
        relative,_=accept_zip(self.root,'synthetic-job',encoded,[{'code':self.card['code']}])
        path,payload=register_synthetic(self.root,self.card)
        saved=(self.root/relative/'artifact.zip').read_bytes()
        content=download_zip(self.root,relative,[{'code':self.card['code']}])
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(len(archive.namelist()),10)
            self.assertTrue(all('/' not in name and '\\' not in name for name in archive.namelist()),archive.namelist())
            self.assertEqual(archive.read('09-999-0001-HighScore-original.rtf'),payload)
            self.assertEqual(archive.read('09-999-0001-Lines.bin'),base64.b64decode(self.card['database_raw']['lines_base64']))
            manifest=json.loads(archive.read('09-999-0001-回执.json'))
            self.assertEqual(manifest['download_layout_version'],1)
            self.assertEqual(manifest['archive_receipt'],receipt)
            self.assertEqual(set(manifest['files']),set(archive.namelist())-{'09-999-0001-回执.json'})
            for name,item in manifest['files'].items():
                self.assertEqual(item['path'],name)
                self.assertEqual(item['size'],len(archive.read(name)))
                self.assertEqual(item['sha256'],hashlib.sha256(archive.read(name)).hexdigest())
            for kind,item in receipt['files'].items():
                self.assertEqual(archive.read(self.card['code']+'.'+kind),(producer/item['path']).read_bytes())
        self.assertEqual((self.root/relative/'artifact.zip').read_bytes(),saved)

    def test_pdf2_thousand_scale_survives_upload_and_download_with_raw_bytes(self):
        import struct
        database=self.root/'thousand.hsrdb'
        lines=struct.pack('<fHfH',3.5,999,4.5,285)
        fixture(database,lines=lines)
        reader=DatabaseReader(database)
        producer=self.root/'thousand-producer';producer.mkdir()
        receipt=DatabaseExporter(producer,reader).export('09-999-0001')
        encoded=package_archive(producer,[{'status':'success','receipt_path':receipt['receipt_path']}])
        expected=[{'code':receipt['code'],'source':receipt['source']}]
        relative,_=accept_zip(self.root,'thousand-job',encoded,expected)
        content=download_zip(self.root,relative,expected)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            card=json.loads(archive.read('09-999-0001.json'))
            self.assertEqual([p['intensity_percent'] for p in card['peaks']],['28.5','100'])
            self.assertEqual(archive.read('09-999-0001-Lines.bin'),lines)
            snapshot=sqlite3.connect(':memory:')
            try:
                snapshot.deserialize(archive.read('09-999-0001-原始记录.sqlite3'))
                self.assertEqual(snapshot.execute('SELECT Lines FROM general_stored').fetchone()[0],lines)
            finally:snapshot.close()
            self.assertTrue(all('/' not in name for name in archive.namelist()))

    def test_multi_card_download_has_only_one_folder_per_card_without_collisions(self):
        producer=self.root/'multi-producer';producer.mkdir()
        receipts=[DatabaseExporter(producer,self.reader).export(code) for code in ('09-999-0001','09-999-0002')]
        encoded=package_archive(producer,[{'status':'success','receipt_path':item['receipt_path']} for item in receipts])
        expected=[{'code':item['code']} for item in receipts]
        relative,_=accept_zip(self.root,'multi-job',encoded,expected)
        content=download_zip(self.root,relative,expected)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names=archive.namelist()
            self.assertEqual(len(names),18)
            self.assertEqual(len(names),len(set(names)))
            self.assertTrue(all(name.count('/')==1 for name in names),names)
            self.assertEqual({name.split('/')[0] for name in names},{item['code'] for item in receipts})
            for receipt in receipts:
                code=receipt['code'];manifest=json.loads(archive.read(f'{code}/{code}-回执.json'))
                self.assertEqual(manifest['code'],code)
                for item in manifest['files'].values():
                    self.assertTrue(item['path'].startswith(code+'/'))
                    self.assertEqual(hashlib.sha256(archive.read(item['path'])).hexdigest(),item['sha256'])

    def test_legacy_archive_without_database_extras_is_also_flat(self):
        from test_archive import synthetic_card
        from highscore_downloader.archive import ArchiveStore
        producer=self.root/'legacy-producer';producer.mkdir()
        rtf=producer/'native.rtf';rtf.write_bytes(b'{\\rtf1 synthetic unchanged original}')
        card=synthetic_card();receipt=ArchiveStore(producer).save(card,rtf,card['code'])
        encoded=package_archive(producer,[{'status':'success','receipt_path':receipt['receipt_path']}])
        relative,_=accept_zip(self.root,'legacy-job',encoded,[{'code':card['code']}])
        with zipfile.ZipFile(io.BytesIO(download_zip(self.root,relative,[{'code':card['code']}]))) as archive:
            self.assertEqual(len(archive.namelist()),5)
            self.assertTrue(all('/' not in name for name in archive.namelist()))
            self.assertEqual(archive.read(card['code']+'.rtf'),rtf.read_bytes())

    def test_native_route_checks_owner_login_card_and_bytes(self):
        import test_web_api
        from fastapi.testclient import TestClient
        h=test_web_api.WebApiTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        user=h.register();h.enable(user)
        job=h.client.post('/highscore/api/jobs/archive',headers={'X-CSRF-Token':user['csrf_token']},
                          json={'codes':self.card['code'],'send_mail':False,'request_key':'native-download'}).json()
        claim=h.claim().json();producer=self.root/'api-producer';producer.mkdir()
        receipt=DatabaseExporter(producer,self.reader).export(self.card['code'])
        encoded=package_archive(producer,[{'status':'success','receipt_path':receipt['receipt_path']}])
        response=h.complete(claim,result={'archive_status':'success','mail_status':'not_requested'},artifact_zip_base64=encoded)
        self.assertEqual(response.status_code,200,response.text)
        path,payload=register_synthetic(h.root,self.card)
        url=f"/highscore/api/jobs/{job['id']}/native-download?code={self.card['code']}"
        self.assertEqual(h.client.get(url).content,payload)
        self.assertEqual(h.client.get(url.replace(self.card['code'],'09-999-9999')).status_code,404)
        with TestClient(h.app) as anonymous:self.assertEqual(anonymous.get(url).status_code,401)
        h.register('other@example.com')
        self.assertEqual(h.client.get(url).status_code,404)


if __name__=='__main__':unittest.main()
