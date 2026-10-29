"""数据库集合升级仍核查原自动角色投递，不改写历史账本。"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_database_executor
from highscore_database.reader import DatabaseError,OrderedDatabaseReader,load_reader
from highscore_database.executor import JobHandler,isolation_root,report_root


class LegacyIdentityTests(unittest.TestCase):
    def setUp(self):test_database_executor.ExecutorTests.setUp(self)
    def job(self,**values):return test_database_executor.ExecutorTests.job(self,**values)

    def test_old_auto_mail_identity_remains_visible_after_database_set_changes(self):
        old=OrderedDatabaseReader([self.reader]);old_config=replace(self.config,database_id=old.database_id)
        old_handler=JobHandler(old_config,reader=old,mail_settings_loader=self.handler.mail_settings_loader,
                               mail_factory=self.handler.mail_factory)
        self.smtp.send_message.side_effect=OSError('synthetic interrupted submit')
        old_result=old_handler(self.job(database_id=old.database_id,payload={'codes':'09-999-0001','send_mail':True}),threading.Event(),lambda _:None)
        self.assertEqual(old_result['result']['mail_status'],'unknown')
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='.lock'}
        # Different new automatic identity; old aggregate identity is explicitly configured.
        new=OrderedDatabaseReader([self.reader]);new.database_id='hsauto-'+'1'*48
        new.legacy_roots=[self.root];new.legacy_database_ids=[old.database_id]
        root=report_root(isolation_root(self.root,'synthetic-user',new.database_id))
        with patch('highscore_downloader.mailer.MailService._locked',side_effect=AssertionError('historical writes forbidden')):
            self.assertTrue(self.handler._uncertain_legacy_mail(root,old_result['result']['results'],
                'receiver@example.com',threading.Event(),reader=new,user_id='synthetic-user'))
        self.assertTrue(all(p.read_bytes()==data for p,data in before.items()))

    def test_config_accepts_explicit_old_aggregate_and_rejects_arbitrary_ids(self):
        config={'databases':[{'path':self.reader.path.name}],'legacy_output_roots':['.'],
                'legacy_database_ids':['hsauto-'+'a'*48]}
        target=self.root/'database.local.json';target.write_text(json.dumps(config),encoding='utf-8')
        reader=load_reader(self.root);self.assertEqual(reader.legacy_database_ids,config['legacy_database_ids'])
        for invalid in ('../outside','hsauto-wrong',123):
            config['legacy_database_ids']=[invalid];target.write_text(json.dumps(config),encoding='utf-8')
            with self.assertRaises(DatabaseError):load_reader(self.root)


if __name__=='__main__':unittest.main()
