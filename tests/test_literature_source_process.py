"""真实 spawn 进程中的阶段回传与获取轮次回传。"""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from highscore_literature.worker import Worker
from process_fixtures import ProcessEngine, TITLE

ROOT = Path(__file__).resolve().parents[1]


class SourceProcessTests(unittest.TestCase):
    def test_real_child_progress_and_fallback_keep_current_revision(self):
        for mode in ('progress', 'raise'):
            with self.subTest(mode=mode):
                base = ROOT/'.cache/tests'
                base.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='source-process-', dir=base) as directory:
                    root = Path(directory)
                    client = Mock()
                    client.claim.return_value = {'job': {'id': 'current-job', 'title': TITLE,
                        'deadline': time.time()+3600, 'source_revision': 2},
                        'lease_token': 'synthetic-lease', 'lease_seconds': 180}
                    client.heartbeat.return_value = {'cancel_requested': False}
                    worker = Worker(root, client=client, engine=ProcessEngine(root, mode),
                                    accounts_provider=lambda: [{'account_ref': 'synthetic-account'}],
                                    max_seconds=10)
                    self.assertTrue(worker.run_once())
                    values = client.update.call_args.args[2]
                    self.assertEqual(values['source_revision'], 2)
                    if mode == 'progress':
                        self.assertTrue(any(call.kwargs.get('stage') == 'verifying'
                                            for call in client.heartbeat.call_args_list))
                    else:
                        self.assertNotIn('synthetic-private-password', str(values))
