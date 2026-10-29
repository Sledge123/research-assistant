"""历史v3下载按最新标准只读重建，源归档、波长、原始记录和账本保持。"""
from contextlib import closing
import hashlib,io,json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from highscore_database.reader import DatabaseReader
from highscore_database.exporter import prepare_card,METHOD,ORIGIN
from highscore_database.executor import package_archive
from highscore_database import reporting_v3
from highscore_downloader.archive import ArchiveStore
from highscore_web.artifacts import accept_zip
from highscore_web.source_files import download_zip
from test_database_reader import fixture
ROOT=Path(__file__).resolve().parents[1]
class ReportDownloadUpgradeTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'.cache/tests';base.mkdir(parents=True,exist_ok=True);self.temp=tempfile.TemporaryDirectory(prefix='download-v4-',dir=base);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  db=self.root/'source.hsrdb';fixture(db);reader=DatabaseReader(db);self.producer=self.root/'producer';self.producer.mkdir()
  card=prepare_card(reader.card('09-999-0001'),{'wavelength_angstrom':'1.54060','source':'Synthetic explicit wavelength'})
  card['verification']={'method':METHOD,'checked_at':'2026-01-01T00:00:00Z','peak_count':card['peak_count'],'database_sha256':card['database_sha256'],'rtf_origin':ORIGIN,'native_highscore_export':False,'report_version':3,'completeness_scope':'available_database_fields'}
  card['report_fields']=reporting_v3.report_fields(card);card['raw_text']=reporting_v3.report_text(card)
  rtf=self.producer/'old-report.rtf';rtf.write_bytes(reporting_v3.report_rtf(card));store=ArchiveStore(self.producer);receipt=store.save(card,rtf,card['code'])
  receipt.update(export_method=METHOD,rtf_origin=ORIGIN,database_id=card['database_id'],report_version=3);(self.producer/receipt['receipt_path']).write_text(json.dumps(receipt),encoding='utf-8');store.validate(receipt['receipt_path'])
  self.card,self.receipt=card,receipt;self.encoded=package_archive(self.producer,[{'status':'success','receipt_path':receipt['receipt_path']}]);self.expected=[{'code':card['code']}];self.relative,_=accept_zip(self.root,'old-v3',self.encoded,self.expected)
 def files(self):
  return {p.relative_to(self.root/self.relative).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/self.relative).rglob('*') if p.is_file()}
 def test_v3_download_uses_v4_standard_without_overwriting_old_archive(self):
  before=self.files();content=download_zip(self.root,self.relative,self.expected)
  with zipfile.ZipFile(io.BytesIO(content)) as z:
   card=json.loads(z.read(self.card['code']+'.json'))
   self.assertEqual(card['verification']['report_version'],6)
   self.assertEqual(card['database_raw'],self.card['database_raw']);self.assertEqual(card['peaks'],self.card['peaks']);self.assertEqual(card['angular_conversion'],self.card['angular_conversion'])
   self.assertEqual(z.read(self.card['code']+'.txt').decode(),card['raw_text'])
   receipt=json.loads(z.read(self.card['code']+'-回执.json'));self.assertEqual(receipt['report_rendering']['from_version'],3);self.assertEqual(receipt['report_rendering']['to_version'],6)
  self.assertEqual(self.files(),before)
 def test_repeat_download_retains_same_source_and_upgrade_proof(self):
  rows=[]
  for _ in range(2):
   upgrades=[];content=download_zip(self.root,self.relative,self.expected,upgrades=upgrades)
   with zipfile.ZipFile(io.BytesIO(content)) as z:rows.append({n:z.read(n) for n in z.namelist()})
   self.assertEqual(len(upgrades),1);self.assertEqual(upgrades[0]['code'],self.card['code']);self.assertEqual(upgrades[0]['from_version'],3)
  self.assertEqual(rows[0],rows[1])
 def test_api_upgrade_is_audited_once_without_extra_charge(self):
  import test_web_api
  h=test_web_api.WebApiTests('runTest');h.setUp();self.addCleanup(h.doCleanups);user=h.register();h.enable(user)
  response=h.client.post(test_web_api.BASE+'/jobs/archive',headers={'X-CSRF-Token':user['csrf_token']},json={'codes':self.card['code'],'send_mail':False,'request_key':'upgrade-original'})
  self.assertEqual(response.status_code,200,response.text);job=response.json();claim=h.claim().json();done=h.complete(claim,result={'archive_status':'success','mail_status':'not_requested'},artifact_zip_base64=self.encoded);self.assertEqual(done.status_code,200,done.text)
  wallet=h.client.get(test_web_api.BASE+'/credits').json()
  for _ in range(2):
   response=h.client.get(test_web_api.BASE+'/jobs/'+job['id']+'/download');self.assertEqual(response.status_code,200,response.text)
   with zipfile.ZipFile(io.BytesIO(response.content)) as z:self.assertEqual(json.loads(z.read(self.card['code']+'.json'))['verification']['report_version'],6)
  self.assertEqual(h.client.get(test_web_api.BASE+'/credits').json(),wallet)
  with h.db() as db:
   rows=db.execute("SELECT detail FROM audit WHERE user_id=? AND action='xrd_download_report_upgraded'",(user['id'],)).fetchall()
  self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0]['detail'])['job_id'],job['id'])
if __name__=='__main__':unittest.main()
