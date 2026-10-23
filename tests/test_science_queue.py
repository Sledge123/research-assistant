"""真实SQLite短事务下跨工具互斥、公平领取和到期接续。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest

from highscore_web.abtem_store import AbtemStore
from highscore_web.pymatgen_store import PymatgenStore
from highscore_web.database import Database
from highscore_web import science_queue
from test_abtem_store import upload as abtem_upload
from test_pymatgen_api import upload

ROOT = Path(__file__).resolve().parents[1]


class ScienceQueueTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='science-queue-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = Database(self.root)
        with self.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u1','synthetic@example.test','unused',1,0,?)", (time.time(),))
        self.abtem = AbtemStore(self.root, self.database, enabled=True)
        self.pymatgen = PymatgenStore(self.root, self.database, enabled=True)
        self.abtem.science_peers, self.pymatgen.science_peers = (self.pymatgen,), (self.abtem,)
        with self.database.connection() as db:
            self.abtem_input = self.abtem.input(db, 'u1', abtem_upload(), {})
            self.pymatgen_input = self.pymatgen.resolve_inputs(db, 'u1', [upload()], 'convert', {})
        with self.database.connection(write=True) as db:
            for kind in ('abtem', 'pymatgen'):
                db.execute(f'INSERT INTO {kind}_workers VALUES (?,?,1)', ('worker-' + kind, time.time()))

    def enqueue_both(self):
        with self.database.connection(write=True) as db:
            first = self.abtem.enqueue(db, 'u1', 'abtem-job', self.abtem_input)
            second = self.pymatgen.enqueue(db, 'u1', 'pymatgen-job', self.pymatgen_input)
            db.execute('UPDATE abtem_jobs SET created_at=?', (time.time() - 5,))
        return first, second

    def claim(self, service):
        with self.database.connection(write=True) as db:
            return service.claim(db, 'worker-test', True)

    def test_older_runnable_type_claims_first_and_global_running_is_one(self):
        self.enqueue_both()
        self.assertIsNone(self.claim(self.pymatgen)['job'])
        claimed = self.claim(self.abtem)
        self.assertIsNotNone(claimed['job']); self.assertIsNone(self.claim(self.pymatgen)['job'])
        with self.database.connection(write=True) as db:
            self.abtem.complete(db, claimed['job']['id'], claimed['lease_token'], 'failed')
        self.assertIsNotNone(self.claim(self.pymatgen)['job'])

    def test_simultaneous_cross_type_claims_cannot_both_enter_running(self):
        self.enqueue_both()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(self.claim, (self.abtem, self.pymatgen)))
        self.assertEqual(sum(claim['job'] is not None for claim in claims), 1)
        with self.database.connection() as db:
            count = sum(db.execute(f"SELECT COUNT(*) FROM {kind}_jobs WHERE state='running'").fetchone()[0]
                        for kind in ('abtem', 'pymatgen'))
            self.assertEqual(count, 1)

    def test_offline_older_queue_does_not_block_runnable_other_type(self):
        self.enqueue_both()
        with self.database.connection(write=True) as db:
            db.execute('UPDATE abtem_workers SET available=0')
        self.assertIsNotNone(self.claim(self.pymatgen)['job'])

    def test_peer_expiry_ends_business_state_once_before_new_type_claim(self):
        first, _ = self.enqueue_both(); claim = self.claim(self.abtem)
        with self.database.connection(write=True) as db:
            db.execute('UPDATE abtem_jobs SET lease_until=? WHERE id=?', (time.time() - 1, first['id']))
        self.assertIsNotNone(self.claim(self.pymatgen)['job'])
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM abtem_jobs').fetchone()[0], 'timed_out')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_entries WHERE kind='abtem_refund'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_entries WHERE kind='pymatgen_freeze'").fetchone()[0], 1)
        self.assertIsNone(self.claim(self.abtem)['job'])

    def test_shared_claim_requires_active_transaction(self):
        with self.database.connection() as db, self.assertRaises(RuntimeError):
            science_queue.claimable(db, 'pymatgen')


if __name__ == '__main__':
    unittest.main()
