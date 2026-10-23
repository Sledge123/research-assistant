"""逐库接续的行为与真实来源隔离；测试库均为 synthetic。"""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_database_reader
import test_database_executor
from test_database_reader import fixture
from highscore_database.reader import DatabaseReader, DatabaseError, CardMissing, load_reader


class FallbackReaderTests(unittest.TestCase):
    def setUp(self):test_database_reader.DatabaseReaderTests.setUp(self)

    def ordered(self):
        from highscore_database.reader import OrderedDatabaseReader
        paths=[self.database,self.root/'second.hsrdb',self.root/'third.hsrdb']
        for index,path in enumerate(paths):
            if index:fixture(path,count=1)
            with closing(sqlite3.connect(path)) as db,db:
                db.execute("UPDATE Properties SET Value=? WHERE Key='Description'",(f'Synthetic {index+1}',))
                db.execute('UPDATE general_indexed SET ProductID=? WHERE id=1',(f'09999000{index+1}',))
                if index==0:db.execute('DELETE FROM general_indexed WHERE id=2');db.execute("UPDATE Properties SET Value='1' WHERE Key='NoOfPatterns'")
                db.execute('UPDATE general_text_content SET c1compoundname=?',(f'Compound {index+1}',))
        self.readers=[DatabaseReader(path) for path in paths]
        return OrderedDatabaseReader(self.readers)

    def test_card_falls_through_all_readers_and_keeps_actual_fingerprint(self):
        reader=self.ordered()
        for index in range(3):
            card=reader.card(f'09-999-000{index+1}')
            self.assertEqual(card['database_id'],self.readers[index].database_id)
            self.assertEqual(card['source'],self.readers[index].source)
        with self.assertRaises(CardMissing):reader.card('09-999-9999')

    def test_search_stops_at_first_matching_database(self):
        reader=self.ordered()
        with patch.object(self.readers[2],'search',side_effect=AssertionError('命中后不得继续')):
            self.assertEqual(reader.search('compound_name','Compound 2')[0]['source'],'Synthetic 2')
        self.assertEqual(reader.search('compound_name','Missing'),[])

    def test_error_is_not_treated_as_missing_and_stop_propagates(self):
        reader=self.ordered()
        with patch.object(self.readers[0],'card',side_effect=DatabaseError('synthetic invalid')):
            with self.assertRaises(DatabaseError):reader.card('09-999-0002')
        stop=threading.Event();stop.set();reader.stop_event=stop
        with self.assertRaises(DatabaseError):reader.card('09-999-0002')

    def test_order_is_bound_to_fingerprint_and_duplicate_sources_are_rejected(self):
        from highscore_database.reader import OrderedDatabaseReader
        reader=self.ordered()
        self.assertNotEqual(reader.database_id,OrderedDatabaseReader(list(reversed(self.readers))).database_id)
        with self.assertRaises(DatabaseError):OrderedDatabaseReader([self.readers[0],self.readers[0]])

    def test_project_config_loads_all_databases_in_order(self):
        self.ordered()
        (self.root/'database.local.json').write_text(json.dumps({'databases':[{'path':p.path.name} for p in self.readers]}),encoding='utf-8')
        reader=load_reader(self.root)
        self.assertEqual(reader.card('09-999-0003')['source'],'Synthetic 3')

    def test_first_card_match_wins_without_querying_later_database(self):
        reader=self.ordered()
        with patch.object(self.readers[1],'card',side_effect=AssertionError('命中后不得继续')):
            self.assertEqual(reader.card('09-999-0001')['source'],'Synthetic 1')

    def test_invalid_configuration_does_not_silently_skip_database(self):
        self.ordered()
        (self.root/'database.local.json').write_text(json.dumps({'databases':[
            {'path':self.readers[0].path.name},{'path':'missing.hsrdb'}]}),encoding='utf-8')
        with self.assertRaises(DatabaseError):load_reader(self.root)


class FallbackExecutorTests(unittest.TestCase):
    def setUp(self):test_database_executor.ExecutorTests.setUp(self)
    def job(self,**values):return test_database_executor.ExecutorTests.job(self,**values)
    def test_mixed_batch_revalidates_real_sources_and_reuses_reports(self):
        from highscore_database.reader import OrderedDatabaseReader
        from highscore_database.executor import JobHandler
        from highscore_web.artifacts import accept_zip,checked_zip
        second=self.root/'second.hsrdb';fixture(second,count=1)
        with closing(sqlite3.connect(second)) as db,db:
            db.execute("UPDATE Properties SET Value='Synthetic second' WHERE Key='Description'")
            db.execute("UPDATE general_indexed SET ProductID='099990003'")
        reader=OrderedDatabaseReader([self.reader,DatabaseReader(second)])
        config=replace(self.config,database_id=reader.database_id)
        handler=JobHandler(config,reader=reader)
        job=self.job(database_id=reader.database_id,payload={'codes':'09-999-0001 09-999-0003 09-999-9999','send_mail':False})
        for _ in range(2):
            output=handler(job,threading.Event(),lambda _:None)
            self.assertEqual(output['result']['archive_status'],'partial')
            self.assertEqual([r['status'] for r in output['result']['results']],['success','success','not_found'])
            relative,receipts=accept_zip(self.root,'mixed-upload',output['artifact_zip_base64'],[{'code':'09-999-0001'},{'code':'09-999-0003'}])
            self.assertEqual({r['source'] for r in receipts},{self.reader.source,'Synthetic second'})
            self.assertTrue(checked_zip(self.root,relative,[{'code':r['code']} for r in receipts]))

    def test_candidate_keeps_actual_source_even_when_first_database_has_same_code(self):
        from highscore_database.reader import OrderedDatabaseReader
        from highscore_database.executor import JobHandler
        second=self.root/'second.hsrdb';fixture(second,count=1)
        with closing(sqlite3.connect(second)) as db,db:
            db.execute("UPDATE Properties SET Value='Synthetic second' WHERE Key='Description'")
        reader=OrderedDatabaseReader([self.reader,DatabaseReader(second)])
        handler=JobHandler(replace(self.config,database_id=reader.database_id),reader=reader)
        candidate=reader.readers[1].search('formula','X Y')[0]
        job=self.job(database_id=reader.database_id,payload={'codes':candidate['code'],'selected':[candidate],'send_mail':False})
        result=handler(job,threading.Event(),lambda _:None)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['result']['results'][0]['source'],'Synthetic second')

    def test_automatic_worker_checks_uncertain_mail_in_original_database_ledger(self):
        from highscore_database.reader import OrderedDatabaseReader
        from highscore_database.executor import JobHandler
        self.smtp.send_message.side_effect=OSError('synthetic disconnect')
        old=self.handler(self.job(payload={'codes':'09-999-0001','send_mail':True}),threading.Event(),lambda _:None)
        self.assertEqual(old['result']['mail_status'],'unknown')
        reader=OrderedDatabaseReader([self.reader])
        handler=JobHandler(replace(self.config,database_id=reader.database_id),reader=reader,
              mail_settings_loader=self.handler.mail_settings_loader,mail_factory=self.handler.mail_factory)
        current=handler(self.job(id='auto-mail',database_id=reader.database_id,
                    payload={'codes':'09-999-0001','send_mail':True}),threading.Event(),lambda _:None)
        self.assertEqual(current['result']['mail_status'],'unknown')
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_readonly_legacy_mail_never_creates_locks_or_recovers_records(self):
        from highscore_database.reader import OrderedDatabaseReader
        from highscore_database.executor import report_root,isolation_root
        from highscore_downloader.mailer import MailService
        self.smtp.send_message.side_effect=OSError('synthetic disconnect')
        old=self.handler(self.job(payload={'codes':'09-999-0001','send_mail':True}),threading.Event(),lambda _:None)
        previous={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='.lock'}
        reader=OrderedDatabaseReader([self.reader]);reader.legacy_roots=[self.root]
        root=report_root(isolation_root(self.root,'synthetic-user',reader.database_id))
        with patch.object(MailService,'_locked',side_effect=AssertionError('禁止写入旧账本')):
            self.assertTrue(self.handler._uncertain_legacy_mail(root,old['result']['results'],
                    'receiver@example.com',threading.Event(),reader=reader,user_id='synthetic-user'))
        self.assertTrue(all(__import__('pathlib').Path(name).read_bytes()==value for name,value in previous.items()))
