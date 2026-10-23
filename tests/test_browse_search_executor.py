"""执行器只为新版任务调用完整候选浏览接口，搜索不创建报告。"""
from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

import test_database_executor
from test_database_reader import fixture
from highscore_database.executor import JobHandler, isolation_root, report_root
from highscore_database.reader import DatabaseReader, OrderedDatabaseReader
from highscore_web.artifacts import accept_zip, checked_zip


class BrowseSearchExecutorTests(unittest.TestCase):
    def setUp(self):
        self.h = test_database_executor.ExecutorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_version_two_uses_browse_without_exporting_or_mailing(self):
        candidates = [{'code': f'09-999-{i:04d}', 'phase_name': 'Synthetic',
                       'chemical_formula': 'X Y', 'source': self.h.reader.source} for i in range(1, 122)]
        self.h.reader.browse = Mock(return_value=candidates)
        with patch.object(self.h.reader, 'search', side_effect=AssertionError('新版不得走旧上限')):
            result = self.h.run_job(self.h.job(kind='search', payload={'mode': 'codes', 'query': '999-0001', 'search_version': 2}))
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['result']['candidates'], candidates)
        self.h.reader.browse.assert_called_once_with('codes', '999-0001')
        self.assertFalse((self.h.root / 'output').exists())
        self.h.smtp_factory.assert_not_called()

    def test_legacy_search_still_uses_original_method(self):
        self.h.reader.browse = Mock(side_effect=AssertionError('旧任务不得切新版'))
        result = self.h.run_job(self.h.job(kind='search', payload={'mode': 'formula', 'query': 'X Y'}))
        self.assertEqual(result['state'], 'succeeded')
        self.h.reader.browse.assert_not_called()

    def test_ordered_reader_passes_structure_content_to_browse(self):
        self.h.reader.browse_content = Mock(return_value=[])
        result = self.h.run_job(self.h.job(kind='search', payload={
            'mode': 'formula', 'query': 'X Y', 'content': 'structure', 'search_version': 2}))
        self.assertEqual(result['state'], 'succeeded')
        self.h.reader.browse_content.assert_called_once_with('formula', 'X Y', 'structure')

    def test_worker_without_browse_fails_explicitly_instead_of_using_legacy_results(self):
        reader = Mock(spec=['database_id', 'content_type'])
        reader.database_id = self.h.reader.database_id
        reader.content_type = 'diffraction'
        self.h.handler.reader_factory = lambda stop: reader
        result = self.h.run_job(self.h.job(kind='search', payload={
            'mode': 'formula', 'query': 'X Y', 'search_version': 2}))
        self.assertEqual(result['state'], 'needs_attention')
        self.assertIn('尚未支持完整候选', result['result']['message'])
        self.assertNotIn('candidates', result['result'])

    def automatic_handler(self):
        second = self.h.root / 'second-source.hsrdb'
        fixture(second, count=3)
        with sqlite3.connect(second) as db:
            db.execute("UPDATE Properties SET Value='Synthetic second source' WHERE Key='Description'")
            db.execute("UPDATE general_text_content SET c1compoundname='Synthetic source B'")
        reader = OrderedDatabaseReader([self.h.reader, DatabaseReader(second)])
        handler = JobHandler(replace(self.h.config, database_id=reader.database_id), reader=reader,
            mail_settings_loader=self.h.handler.mail_settings_loader, mail_factory=self.h.handler.mail_factory)
        return reader, handler

    def selected_job(self, reader, job_id, bindings, *, send_mail=False):
        selected = [{'code': code, 'source': reader.readers[source].source} for code, source in bindings]
        return self.h.job(id=job_id, database_id=reader.database_id, payload={
            'codes': ' '.join(item['code'] for item in selected), 'selected': selected,
            'search_job_id': 'synthetic-v2-search', 'search_version': 2, 'send_mail': send_mail})

    def test_selected_source_bindings_are_isolated_across_tasks_and_real_exports(self):
        reader, handler = self.automatic_handler()
        base = isolation_root(self.h.root, 'synthetic-user', reader.database_id)
        legacy = self.h.job(id='old-auto', database_id=reader.database_id,
            payload={'codes': '09-999-0001', 'send_mail': False})
        self.assertEqual(handler(legacy, threading.Event(), lambda _: None)['state'], 'succeeded')
        legacy_root = report_root(base)
        old_files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in legacy_root.rglob('*') if p.is_file()}
        roots = []
        # 两次任务来源集合都为 A/B，但同号 C 分别选 A 与 B，不能只对来源集合取摘要。
        for job_id, bindings in (
                ('chosen-a', [('09-999-0001', 0), ('09-999-0002', 1)]),
                ('chosen-b', [('09-999-0001', 1), ('09-999-0002', 0)])):
            job = self.selected_job(reader, job_id, bindings)
            result = handler(job, threading.Event(), lambda _: None)
            self.assertEqual(result['state'], 'succeeded', result)
            output_root = handler._original_root(base, job_id)
            roots.append(output_root)
            expected = job['payload']['selected']
            relative, _ = accept_zip(self.h.root, 'server-' + job_id, result['artifact_zip_base64'], expected)
            self.assertTrue(checked_zip(self.h.root, relative, expected))
            for selected in expected:
                path = output_root / 'output/cards' / selected['code'] / (selected['code'] + '.json')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['source'], selected['source'])
        self.assertNotEqual(roots[0], roots[1])
        self.assertNotIn(legacy_root, roots)
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in old_files.items()))
        repeated = self.selected_job(reader, 'same-bindings-again', [('09-999-0002', 1), ('09-999-0001', 0)])
        self.assertEqual(handler(repeated, threading.Event(), lambda _: None)['state'], 'succeeded')
        self.assertEqual(handler._original_root(base, repeated['id']), roots[0])

    def test_source_isolation_still_blocks_unknown_legacy_delivery_until_explicit_confirmation(self):
        reader, handler = self.automatic_handler()
        old = self.h.job(id='legacy-uncertain', database_id=reader.database_id,
            payload={'codes': '09-999-0001', 'send_mail': True})
        self.h.smtp.send_message.side_effect = OSError('synthetic uncertain submit')
        self.assertEqual(handler(old, threading.Event(), lambda _: None)['result']['mail_status'], 'unknown')
        current = self.selected_job(reader, 'new-source-unknown', [('09-999-0001', 1)], send_mail=True)
        result = handler(current, threading.Event(), lambda _: None)
        self.assertEqual(result['result']['archive_status'], 'success')
        self.assertEqual(result['result']['mail_status'], 'unknown')
        self.assertEqual(self.h.smtp.send_message.call_count, 1)
        retry = self.h.job(id='explicit-source-mail', kind='mail_retry', database_id=reader.database_id,
            payload={'original_job_id': current['id'], 'codes': '09-999-0001',
                     'force_new_delivery': True, 'confirm_unknown': False})
        self.assertEqual(handler(retry, threading.Event(), lambda _: None)['result']['mail_status'], 'unknown')
        self.assertEqual(self.h.smtp.send_message.call_count, 1)
        self.h.smtp.send_message.side_effect = None
        retry['payload']['confirm_unknown'] = True
        self.assertEqual(handler(retry, threading.Event(), lambda _: None)['result']['mail_status'], 'submitted')
        self.assertEqual(self.h.smtp.send_message.call_count, 2)

    def test_new_scope_never_automatically_resends_legacy_sent_mail_and_explicit_retry_works(self):
        reader, handler = self.automatic_handler()
        old = self.h.job(id='legacy-sent', database_id=reader.database_id,
            payload={'codes': '09-999-0001', 'send_mail': True})
        self.assertEqual(handler(old, threading.Event(), lambda _: None)['result']['mail_status'], 'submitted')
        current = self.selected_job(reader, 'new-source-after-sent', [('09-999-0001', 0)], send_mail=True)
        result = handler(current, threading.Event(), lambda _: None)
        self.assertEqual(result['result']['archive_status'], 'success')
        self.assertEqual(result['result']['mail_status'], 'not_started')
        self.assertEqual(self.h.smtp.send_message.call_count, 1)
        retry = self.h.job(id='explicit-after-sent', kind='mail_retry', database_id=reader.database_id,
            payload={'original_job_id': current['id'], 'codes': '09-999-0001',
                     'force_new_delivery': True, 'confirm_unknown': False})
        self.assertEqual(handler(retry, threading.Event(), lambda _: None)['result']['mail_status'], 'submitted')
        self.assertEqual(self.h.smtp.send_message.call_count, 2)


if __name__ == '__main__':
    unittest.main()
