"""管理员身份纠正的真实SQLite与私有状态一致性测试，全部离线。"""
import copy
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from highscore_literature.engine import StateStore
from highscore_literature.metadata import candidates_from_items
from highscore_web.database import Database
from highscore_web.literature_store import SCHEMA
from highscore_web.credits import Credits

ROOT = Path(__file__).resolve().parents[1]
TITLE = 'Synthetic article for a controlled identity correction'
OLD = '10.1234/paper.s001'
NEW = '10.1234/paper'
JOB = 'a'*32


def source(doi, kind):
    return {'DOI': doi, 'type': kind, 'title': [TITLE],
            'container-title': ['Synthetic Journal'] if kind == 'journal-article' else [],
            'issued': {'date-parts': [[2026]]}}


class IdentityRepairTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT/'.cache/tmp'
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory, prefix='identity-repair-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = Database(self.root)
        self.now = time.time()
        self.old_candidate = candidates_from_items([{**source(OLD, 'journal-article'), 'container-title': []}])[0]
        with self.database.connection(write=True) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO users VALUES ('admin','admin@example.test','unused',1,1,?)", (self.now,))
            db.execute("INSERT INTO literature_jobs(id,user_id,title,title_key,canonical_title,doi,state,message,created_at,deadline,credit_status,account_ref,selected_candidate) VALUES (?, 'admin', ?, ?, ?, ?, 'needs_attention', '测试', ?, ?, 'frozen', 'synthetic-account', ?)",
                       (JOB, TITLE, hashlib.sha256(TITLE.encode()).hexdigest(), TITLE, OLD,
                        self.now-100, self.now+3600, json.dumps(self.old_candidate)))
            Credits.entry(db, 'admin', 'freeze', -1, 1, 'freeze:'+JOB, JOB)
        self.store = StateStore(self.root)
        self.store.write('synthetic-account', TITLE, {'doi': OLD, 'phase': 'publish_intent',
            'intent_at': self.now-90, 'metadata_identity': self.old_candidate})
        self.state_path = self.store.entry('synthetic-account', TITLE)/'state.json'
        self.proof = {'schema_version': 1, 'job_id': JOB, 'expected_doi': OLD, 'target_doi': NEW,
            'account_ref': 'synthetic-account', 'checked_at': self.now,
            'complete': True, 'submission_state': 'confirmed_not_submitted', 'matches': [],
            'source_record': source(OLD, 'component'), 'target_record': source(NEW, 'journal-article')}
        self.proof_path = self.root/'proof.json'
        self.save_proof()

    def save_proof(self):
        self.proof_path.write_text(json.dumps(self.proof), encoding='utf-8')

    def run_repair(self, apply=False):
        from highscore_literature.repair import repair_identity
        return repair_identity(self.root, job_id=JOB, expected_doi=OLD, target_doi=NEW,
            proof='proof.json', operator_id='admin', apply=apply)

    def row(self):
        with self.database.connection() as db:
            return dict(db.execute('SELECT * FROM literature_jobs WHERE id=?', (JOB,)).fetchone())

    def ledger(self):
        with self.database.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM credit_entries ORDER BY rowid')]

    def rejects(self):
        from highscore_literature.repair import RepairError
        before = self.state_path.read_bytes(), self.row(), self.ledger()
        with self.assertRaises(RepairError):
            self.run_repair(apply=True)
        self.assertEqual(before, (self.state_path.read_bytes(), self.row(), self.ledger()))

    def test_default_is_read_only_without_journal_or_audit(self):
        before = self.state_path.read_bytes(), self.row(), self.ledger()
        result = self.run_repair()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(before, (self.state_path.read_bytes(), self.row(), self.ledger()))
        self.assertFalse((self.store.base/'identity-repairs').exists())
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 0)

    def test_apply_preserves_task_account_deadline_and_ledger_and_is_idempotent(self):
        before, ledger = self.row(), self.ledger()
        result = self.run_repair(apply=True)
        self.assertEqual(result['status'], 'applied')
        after = self.row()
        for key in ('id', 'user_id', 'title', 'title_key', 'account_ref', 'created_at', 'deadline', 'credit_status'):
            self.assertEqual(before[key], after[key])
        self.assertEqual((after['doi'], after['state']), (NEW, 'queued'))
        state = self.store.read('synthetic-account', TITLE)
        self.assertEqual((state['doi'], state['phase']), (NEW, 'new'))
        self.assertNotIn('intent_at', state)
        self.assertEqual(state['metadata_identity']['doi'], NEW)
        self.assertEqual(self.ledger(), ledger)
        journal = json.loads((self.root/result['journal']).read_text(encoding='utf-8'))
        self.assertEqual(journal['before_state']['phase'], 'publish_intent')
        self.assertEqual(journal['before_job']['doi'], OLD)
        self.assertEqual(journal['phase'], 'committed')
        self.assertEqual(self.run_repair(apply=True)['status'], 'already_applied')
        with self.database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='literature_identity_repair'").fetchone()[0], 1)
        self.assertEqual(self.ledger(), ledger)

    def test_cancelled_and_expired_tasks_are_never_reactivated(self):
        for state, deadline in (('cancelled', self.now+3600), ('needs_attention', self.now-1)):
            with self.subTest(state=state), self.database.connection(write=True) as db:
                db.execute('UPDATE literature_jobs SET state=?,deadline=? WHERE id=?', (state, deadline, JOB))
            self.rejects()

    def test_live_lease_and_external_binding_are_refused(self):
        with self.database.connection(write=True) as db:
            db.execute('UPDATE literature_jobs SET lease_until=? WHERE id=?', (self.now+100, JOB))
        self.rejects()
        with self.database.connection(write=True) as db:
            db.execute("UPDATE literature_jobs SET lease_until=NULL,external_request_id='original-request' WHERE id=?", (JOB,))
        self.rejects()

    def test_unknown_submission_or_matches_or_incomplete_scan_are_refused(self):
        original = copy.deepcopy(self.proof)
        for change in ({'submission_state': 'unknown'}, {'matches': ['existing-request']}, {'complete': False}, {'account_ref': 'another-account'}):
            self.proof = {**original, **change}
            self.save_proof()
            self.rejects()

    def test_stale_proof_and_title_or_doi_mismatch_are_refused(self):
        original = copy.deepcopy(self.proof)
        for change in ({'checked_at': self.now-601}, {'target_doi': '10.1234/other'},
                       {'target_record': {**source(NEW, 'journal-article'), 'title': ['Another article title']}}):
            self.proof = {**original, **change}
            self.save_proof()
            self.rejects()

    def test_correct_source_cannot_be_replaced_or_target_changed_to_component(self):
        self.proof['source_record'] = source(OLD, 'journal-article')
        self.save_proof()
        self.rejects()
        self.proof['source_record'] = source(OLD, 'component')
        self.proof['target_record'] = source(NEW, 'component')
        self.save_proof()
        self.rejects()

    def test_operator_must_still_be_a_verified_administrator(self):
        for verified, admin in ((0, 1), (1, 0)):
            with self.database.connection(write=True) as db:
                db.execute('UPDATE users SET verified=?,is_admin=? WHERE id=?', (verified, admin, 'admin'))
            self.rejects()

    def test_worker_mutex_prevents_repair_during_account_operations(self):
        from highscore_literature.worker import worker_mutex
        with worker_mutex(self.store) as acquired:
            self.assertTrue(acquired)
            self.rejects()

    def test_database_failure_rolls_back_private_state_and_preserves_publish_intent(self):
        with self.database.connection(write=True) as db:
            db.execute("CREATE TRIGGER repair_fail BEFORE UPDATE ON literature_jobs BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        self.rejects()
        journal_paths = list((self.store.base/'identity-repairs').glob('*/state.json'))
        self.assertEqual(len(journal_paths), 1)
        self.assertEqual(json.loads(journal_paths[0].read_text())['phase'], 'aborted')

    def test_project_escape_in_proof_path_is_rejected(self):
        from highscore_literature.repair import RepairError, repair_identity
        with self.assertRaises(RepairError):
            repair_identity(self.root, job_id=JOB, expected_doi=OLD, target_doi=NEW,
                            proof='../outside.json', operator_id='admin')

    def test_process_death_after_private_replace_can_resume_same_correction(self):
        code = '''import json,os,sys
from pathlib import Path
from highscore_literature.engine import StateStore
from highscore_literature.repair import repair_identity
root=Path(sys.argv[1]); original=StateStore._atomic
def crash(self,path,data):
    original(self,path,data)
    if path.name=='state.json' and 'accounts' in path.parts and json.loads(data).get('doi')=='10.1234/paper':os._exit(37)
StateStore._atomic=crash
repair_identity(root,job_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',expected_doi='10.1234/paper.s001',target_doi='10.1234/paper',proof='proof.json',operator_id='admin',apply=True)
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], cwd=ROOT,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(self.row()['doi'], OLD)
        self.assertEqual(self.store.read('synthetic-account', TITLE)['doi'], NEW)
        self.assertEqual(self.run_repair()['status'], 'ready')
        self.assertEqual(self.run_repair(apply=True)['status'], 'applied')
        self.assertEqual(self.row()['doi'], NEW)
        self.assertEqual(self.ledger()[0]['kind'], 'freeze')

    def test_commit_success_then_exception_never_restores_wrong_private_identity(self):
        from highscore_literature.repair import RepairError
        original = sqlite3.connect
        class AmbiguousCommit(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise KeyboardInterrupt('synthetic commit return interruption')
        def connect(*args, **kwargs):
            kwargs['factory'] = AmbiguousCommit
            return original(*args, **kwargs)
        with patch('highscore_literature.repair.sqlite3.connect', side_effect=connect):
            with self.assertRaises(RepairError):
                self.run_repair(apply=True)
        self.assertEqual(self.row()['doi'], NEW)
        self.assertEqual(self.store.read('synthetic-account', TITLE)['doi'], NEW)
        self.assertEqual(self.run_repair(apply=True)['status'], 'already_applied')
        self.assertEqual(len(self.ledger()), 1)

    def test_authoritative_html_and_entity_title_uses_same_cleanup_as_candidates(self):
        self.proof['source_record']['title'] = ['Synthetic <i>article</i> for a controlled identity correction']
        self.proof['target_record']['title'] = ['Synthetic &#97;rticle for a controlled identity correction']
        self.save_proof()
        self.assertEqual(self.run_repair()['status'], 'ready')

    def test_journal_failure_after_commit_preserves_identity_and_can_finish_idempotently(self):
        from highscore_literature.repair import RepairError
        original = StateStore._atomic
        def fail(self, path, data):
            if 'identity-repairs' in path.parts and json.loads(data).get('phase') == 'committed':
                raise OSError('synthetic journal write error')
            return original(self, path, data)
        with patch.object(StateStore, '_atomic', fail), self.assertRaises(RepairError):
            self.run_repair(apply=True)
        self.assertEqual(self.row()['doi'], NEW)
        self.assertEqual(self.store.read('synthetic-account', TITLE)['doi'], NEW)
        self.assertEqual(self.run_repair(apply=True)['status'], 'already_applied')

    def test_changed_evidence_between_precheck_and_write_is_rejected(self):
        from highscore_literature import repair
        original = repair._proof
        calls = []
        def change(*args):
            value = original(*args)
            calls.append(True)
            if len(calls) == 1:
                self.proof['checked_at'] -= 1
                self.save_proof()
            return value
        with patch.object(repair, '_proof', side_effect=change):
            self.rejects()

    def leave_prepared(self, replace_state=False):
        from highscore_literature.repair import _json
        before = self.store.read('synthetic-account', TITLE)
        after = {**before, 'doi': NEW, 'phase': 'new',
                 'metadata_identity': candidates_from_items([self.proof['target_record']])[0]}
        after.pop('intent_at', None)
        repair_id = hashlib.sha256(_json([JOB, OLD, NEW]).encode()).hexdigest()
        path = self.store.base/'identity-repairs'/repair_id/'state.json'
        journal = {'schema_version': 1, 'repair_id': repair_id, 'phase': 'prepared', 'created_at': self.now,
                   'before_job': self.row(), 'before_state': before, 'after_state': after,
                   'before_state_bytes': base64.b64encode(self.state_path.read_bytes()).decode('ascii'),
                   'original_entries': self.ledger(), 'proof': self.proof}
        self.store._atomic(path, _json(journal).encode())
        if replace_state:
            self.store._atomic(self.state_path, _json(after).encode())
        return path

    def test_prepared_only_is_read_only_until_apply_and_then_resumes(self):
        path = self.leave_prepared()
        before = path.read_bytes(), self.state_path.read_bytes(), self.row()
        self.assertEqual(self.run_repair()['status'], 'ready')
        self.assertEqual(before, (path.read_bytes(), self.state_path.read_bytes(), self.row()))
        self.assertEqual(self.run_repair(apply=True)['status'], 'applied')

    def test_interrupted_repair_refuses_cancellation_changed_ledger_and_unknown_state(self):
        path = self.leave_prepared(replace_state=True)
        with self.database.connection(write=True) as db:
            db.execute("UPDATE literature_jobs SET state='cancelled' WHERE id=?", (JOB,))
        self.rejects()
        with self.database.connection(write=True) as db:
            db.execute("UPDATE literature_jobs SET state='needs_attention' WHERE id=?", (JOB,))
            Credits.entry(db, 'admin', 'refund', 1, -1, 'settle:'+JOB, JOB)
        self.rejects()
        with self.database.connection(write=True) as db:
            db.execute("DELETE FROM credit_entries WHERE kind='refund'")
        state = self.store.read('synthetic-account', TITLE)
        state['updated_at'] += 1
        self.store._atomic(self.state_path, json.dumps(state).encode())
        self.rejects()
        self.assertEqual(json.loads(path.read_text())['phase'], 'prepared')

    def test_second_interruption_preserves_recognizable_after_snapshot(self):
        from highscore_literature.repair import RepairError
        path = self.leave_prepared(replace_state=True)
        original = StateStore._atomic
        def fail(self, target, data):
            original(self, target, data)
            if target == path and json.loads(data).get('phase') == 'prepared':
                raise OSError('synthetic second interruption before state replace')
        with patch.object(StateStore, '_atomic', fail), self.assertRaises(RepairError):
            self.run_repair(apply=True)
        self.assertEqual(self.run_repair()['status'], 'ready')
        self.assertEqual(self.run_repair(apply=True)['status'], 'applied')


if __name__ == '__main__':
    unittest.main()
