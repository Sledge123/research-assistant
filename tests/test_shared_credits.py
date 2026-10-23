"""全站共用积分：跨文献/XRD冻结、文件复验结算、幂等及非管理员使用。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from fastapi.testclient import TestClient
from highscore_web import create_app
from test_literature_api import sample_pdf,TITLE

BASE='/highscore/api';ROOT=Path(__file__).resolve().parents[1]

class SharedCreditTests(unittest.TestCase):
    def setUp(self):
        cache=ROOT/'.cache/tmp';cache.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=cache,prefix='shared-credit-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.app=create_app(self.root,{'public_url':'http://testserver/highscore/','secure_cookie':False,
            'database_admin_only':False,'worker_token':'synthetic-test-worker-secret-123456',
            'literature_worker_token':'synthetic-literature-credit-token-123456'})
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.h={'X-CSRF-Token':'synthetic-csrf'}
        with self.app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u1','credits@example.test','unused',1,0,?)",(time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(b'synthetic-session').hexdigest(),'u1','synthetic-csrf',time.time()+3600))
            db.execute("UPDATE settings SET value='true' WHERE key IN ('license_confirmed','cloud_validated','public_enabled','literature_enabled')")
        self.client.cookies.set('highscore_session','synthetic-session')
        from test_web_api import WebApiTests
        self.fx=WebApiTests();self.fx.root=self.root;self.fx.client=self.client
        self.fx.worker={'Authorization':'Bearer synthetic-test-worker-secret-123456'}
    def wallet(self):return self.client.get(BASE+'/credits').json()
    def archive(self,codes='09-999-0001',key='xrd'):
        return self.client.post(BASE+'/jobs/archive',headers=self.h,json={'codes':codes,'send_mail':False,'request_key':key})
    def literature(self,key='lit'):
        return self.client.post(BASE+'/literature/jobs',headers=self.h,json={'title':TITLE+' '+key,'request_key':key})
    def finish(self,claim,codes=None,**values):
        result={'archive_status':'success','mail_status':'not_requested'};result.update(values.pop('result',{}))
        return self.fx.complete(claim,result=result,artifact_zip_base64=base64.b64encode(self.fx.archive_zip(codes=codes)).decode(),**values)
    def test_verified_non_admin_success_charges_each_card_once_and_downloads_free(self):
        self.assertFalse(self.client.get(BASE+'/auth/me').json()['is_admin'])
        first=self.archive('09-999-0001 09-999-0002');self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.archive('09-999-0001 09-999-0002').json()['id'],first.json()['id'])
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,2))
        claim=self.fx.claim().json();done=self.finish(claim,['09-999-0001','09-999-0002']);self.assertEqual(done.status_code,200,done.text)
        self.assertEqual(self.finish(claim,['09-999-0001','09-999-0002']).status_code,409)
        for _ in range(2):self.assertEqual(self.client.get(BASE+'/jobs/'+first.json()['id']+'/download').status_code,200)
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,0))
        self.assertEqual(self.client.get(BASE+'/jobs/'+first.json()['id']).json()['credits']['charged'],2)
        create_app(self.root,{'public_url':'http://testserver/highscore/','secure_cookie':False})
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,0))
    def test_partial_only_verified_card_charged_and_invalid_package_cannot_charge(self):
        job=self.archive('09-999-0001 09-999-0002').json();claim=self.fx.claim().json()
        bad=self.fx.complete(claim,result={'archive_status':'success'},artifact_zip_base64=base64.b64encode(b'bad').decode())
        self.assertEqual(bad.status_code,422)
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,2))
        done=self.finish(claim,['09-999-0001'],state='failed',result={'archive_status':'partial'})
        self.assertEqual(done.status_code,200,done.text)
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(9,0))
        self.assertEqual(self.client.get(BASE+'/jobs/'+job['id']).json()['credits']['charged'],1)
    def test_cancel_failure_and_expired_lease_refund_without_duplicate_entries(self):
        for number in range(3):
            job=self.archive(key=str(number)).json()
            if number==0:
                for _ in range(2):self.client.post(BASE+'/jobs/'+job['id']+'/cancel',headers=self.h)
            else:
                claim=self.fx.claim().json()
                if number==1:self.fx.complete(claim,state='failed',result={'archive_status':'failed'})
                else:
                    with self.app.state.database.connection(write=True) as db:db.execute('UPDATE jobs SET lease_expires=0 WHERE id=?',(job['id'],))
                    self.client.get(BASE+'/jobs/'+job['id'])
            self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(10,0))
    def test_literature_and_xrd_use_one_balance_and_one_registration_grant(self):
        literature=self.literature().json();self.archive()
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,2))
        headers={'Authorization':'Bearer synthetic-literature-credit-token-123456'}
        claim=self.client.post(BASE+'/worker/literature/claim',headers=headers,json={'worker_id':'synthetic-lit','accounts_ready':True}).json()
        response=self.client.post(BASE+'/worker/literature/jobs/'+literature['id']+'/update',headers=headers,json={
            'lease_token':claim['lease_token'],'state':'succeeded','canonical_title':TITLE+' lit','pdf_base64':base64.b64encode(sample_pdf(TITLE+' lit')).decode()})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.finish(self.fx.claim().json()).status_code,200)
        wallet=self.wallet();self.assertEqual((wallet['available'],wallet['frozen']),(8,0))
        self.assertEqual(len([row for row in wallet['items'] if row['kind']=='registration']),1)
        self.assertEqual({row.get('job_kind') for row in wallet['items'] if row['job_id']},{'xrd','literature'})
    def test_concurrent_cross_feature_last_point_cannot_be_overdrawn(self):
        for number in range(9):self.assertEqual(self.literature('reserve'+str(number)).status_code,200)
        with ThreadPoolExecutor(max_workers=2) as pool:
            xrd=pool.submit(self.archive);lit=pool.submit(self.literature,'last')
            self.assertEqual(sorted([xrd.result().status_code,lit.result().status_code]),[200,402])
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(0,10))
    def test_search_and_mail_retry_do_not_reserve_points_and_legacy_not_backcharged(self):
        search=self.client.post(BASE+'/jobs/search',headers=self.h,json={'mode':'formula','query':'X','request_key':'search'}).json()
        self.assertEqual(self.wallet()['frozen'],0)
        self.fx.complete(self.fx.claim().json(),result={'candidates':[]})
        job=self.archive().json();claim=self.fx.claim().json();self.assertEqual(self.finish(claim).status_code,200)
        before=self.wallet()['available']
        mail=self.client.post(BASE+'/jobs/'+job['id']+'/mail-retry',headers=self.h,json={'request_key':'mail'})
        self.assertEqual(mail.status_code,200,mail.text)
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(before,0))

    def test_legacy_job_without_reservation_is_not_backcharged(self):
        job=self.archive().json()
        with self.app.state.database.connection(write=True) as db:
            db.execute('DELETE FROM xrd_credit_holds WHERE job_id=?',(job['id'],))
            db.execute("DELETE FROM credit_entries WHERE job_id=? AND kind='xrd_freeze'",(job['id'],))
        self.assertEqual(self.finish(self.fx.claim().json()).status_code,200)
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(10,0))

    def test_missing_cards_finish_with_clear_message_and_refund_once(self):
        job=self.archive('09-999-0098 09-999-0099').json();claim=self.fx.claim().json()
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(8,2))
        result={'archive_status':'failed','mail_status':'not_requested',
                'message':'本批没有通过完整校验的卡片；未提交邮件。',
                'results':[{'code':code,'status':'not_found','message':'数据库未找到该编号。'}
                           for code in ('09-999-0098','09-999-0099')]}
        done=self.fx.complete(claim,state='needs_attention',result=result)
        self.assertEqual(done.status_code,200,done.text)
        current=self.client.get(BASE+'/jobs/'+job['id']).json()
        self.assertEqual(current['state'],'failed')
        self.assertEqual(current['message'],'未发现该卡片信息，已返还2积分。')
        self.assertEqual(current['credits'],{'reserved':2,'charged':0,'status':'refunded'})
        self.assertFalse(current['download_available'])
        self.assertTrue(all(item['status']=='not_found' and item['message']=='未发现该卡片信息'
                            for item in current['results']))
        self.assertEqual(self.fx.complete(claim,state='needs_attention',result=result).status_code,409)
        self.assertEqual(self.archive('09-999-0098 09-999-0099').json()['id'],job['id'])
        wallet=self.wallet();self.assertEqual((wallet['available'],wallet['frozen']),(10,0))
        self.assertEqual(len([item for item in wallet['items'] if item['kind']=='xrd_refund']),1)

    def test_partial_missing_card_refunds_only_unsuccessful_part(self):
        job=self.archive('09-999-0001 09-999-0099').json();claim=self.fx.claim().json()
        done=self.finish(claim,['09-999-0001'],state='needs_attention',result={
            'archive_status':'partial','message':'本批部分卡片通过完整校验。',
            'results':[{'code':'09-999-0099','status':'not_found'}]})
        self.assertEqual(done.status_code,200,done.text)
        current=self.client.get(BASE+'/jobs/'+job['id']).json()
        self.assertIn('未发现该卡片信息：09-999-0099',current['message'])
        self.assertIn('已返还1积分',current['message'])
        self.assertEqual(current['credits'],{'reserved':2,'charged':1,'status':'partial'})
        self.assertTrue(current['download_available'])
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(9,0))
        self.assertEqual([item['status'] for item in current['results']],['success','not_found'])

    def test_empty_search_reports_missing_without_credit_activity(self):
        job=self.client.post(BASE+'/jobs/search',headers=self.h,json={
            'mode':'formula','query':'Missing','request_key':'empty-search'}).json()
        done=self.fx.complete(self.fx.claim().json(),result={'candidates':[],'message':'检索完成，共 0 个候选。'})
        self.assertEqual(done.status_code,200,done.text)
        current=self.client.get(BASE+'/jobs/'+job['id']).json()
        self.assertEqual(current['message'],'未发现该卡片信息')
        self.assertEqual(current['state'],'succeeded')
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(10,0))
        self.assertFalse(any(item['kind'].startswith('xrd_') for item in self.wallet()['items']))

    def test_legacy_missing_job_does_not_claim_a_refund(self):
        job=self.archive('09-999-0099').json()
        with self.app.state.database.connection(write=True) as db:
            db.execute('DELETE FROM xrd_credit_holds WHERE job_id=?',(job['id'],))
            db.execute("DELETE FROM credit_entries WHERE job_id=? AND kind='xrd_freeze'",(job['id'],))
        done=self.fx.complete(self.fx.claim().json(),state='needs_attention',result={
            'archive_status':'failed','results':[{'code':'09-999-0099','status':'not_found'}]})
        self.assertEqual(done.status_code,200,done.text)
        current=self.client.get(BASE+'/jobs/'+job['id']).json()
        self.assertEqual(current['message'],'未发现该卡片信息')
        self.assertEqual(current['credits']['status'],'not_required')
        self.assertEqual((self.wallet()['available'],self.wallet()['frozen']),(10,0))

if __name__=='__main__':unittest.main()
