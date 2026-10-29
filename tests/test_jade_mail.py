"""CIF进入独立邮件指纹/附件；离线验证不连接SMTP。"""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from highscore_downloader.mail_settings import MailSettings
from highscore_downloader.mailer import MailService,_fingerprint,_json

ROOT=Path(__file__).resolve().parents[1]


class JadeMailTests(unittest.TestCase):
    def test_legacy_four_file_fingerprint_is_unchanged_and_cif_is_bound(self):
        files={kind:{'sha256':hashlib.sha256(kind.encode()).hexdigest()} for kind in ('rtf','txt','json','csv')}
        expected=hashlib.sha256(_json({'recipient':'r@example.com','code':'00-030-0820',
            'files':{kind:files[kind]['sha256'] for kind in ('rtf','txt','json','csv')}})).hexdigest()
        self.assertEqual(_fingerprint('r@example.com','00-030-0820',files),expected)
        files['cif']={'sha256':'a'*64};first=_fingerprint('r@example.com','ICSD-000001',files)
        files['cif']['sha256']='b'*64
        self.assertNotEqual(_fingerprint('r@example.com','ICSD-000001',files),first)

    def test_structure_dry_run_contains_cif_and_never_sends(self):
        from test_jade_structure_reader import fixture_source
        from highscore_database.jade_structure_build import build_database
        from highscore_database.jade_structure import JadeStructureReader,FORMAT
        from highscore_database.exporter import DatabaseExporter
        cache=ROOT/'.cache/tests';cache.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='jade-mail-',dir=cache) as directory:
            root=Path(directory);source=fixture_source(root/'source');database=root/'source.sqlite3'
            built=build_database(source,database)
            reader=JadeStructureReader(database,profile={'format':FORMAT,'source':'Synthetic ICSD','sha256':built['sha256']})
            producer=root/'producer';producer.mkdir();receipt=DatabaseExporter(producer,reader).export('ICSD-000001')
            service=MailService(producer,MailSettings(username='synthetic@example.com',authorization_code='synthetic-only'))
            with patch.object(socket,'create_connection',side_effect=AssertionError('offline must not connect')), \
                 patch.object(socket.socket,'connect',side_effect=AssertionError('offline must not connect')):
                result=service.deliver([{'status':'success','actual_code':'ICSD-000001','receipt_path':receipt['receipt_path']}],
                    'receiver@example.com',dry_run=True)
                self.assertTrue(result['dry_run'])
                job=service._read_job(result['job_id'])
                self.assertEqual(job['status'],'prepared')
                self.assertIn('cif',job['cards'][0]['files'])
                self.assertIn('原子位点数：9',service._body(job))
                with zipfile.ZipFile(producer/job['zip_path']) as archive:
                    self.assertEqual(archive.read('ICSD-000001/ICSD-000001.cif'),
                        (producer/receipt['files']['cif']['path']).read_bytes())
                service._validate_package(job,check_source=True)


if __name__=='__main__':unittest.main()
