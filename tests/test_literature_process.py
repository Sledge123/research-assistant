"""真实离线子进程回归；临时目录和进程回执均留在项目内。"""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from highscore_literature.engine import LeaseLost, LiteratureEngine, StateStore
from highscore_literature.worker import LeaseGuard, Worker
from process_fixtures import BrowserFactory, ProcessEngine, TITLE, cleanup_receipt_after_release


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = {'account_ref': 'synthetic-a', 'username': 'synthetic@example.test',
           'password': 'synthetic-private-password', 'enabled': True}


def alive(pid):
    if os.name == 'nt':
        import ctypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.restype = ctypes.c_void_p
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            api.CloseHandle(handle)
    try:
        state = Path(f'/proc/{pid}/stat')
        if state.exists() and state.read_text().split(') ', 1)[1].startswith('Z'):
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class LiteratureProcessTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / '.cache/tests'
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='literature-process-', dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Mock()
        self.job = {'id': 'synthetic-job', 'title': TITLE, 'canonical_title': TITLE, 'doi': None,
                    'external_request_id': None, 'account_ref': None, 'deadline': time.time() + 3600}
        self.client.claim.return_value = {'job': self.job, 'lease_token': 'synthetic-lease', 'lease_seconds': .09}
        self.client.heartbeat.return_value = {'cancel_requested': False}

    @property
    def marker(self):
        target = self.root / 'output/literature-worker/process-marker.json'
        try:
            return json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def worker(self, engine=None, seconds=5):
        return Worker(self.root, client=self.client, engine=engine,
                      accounts_provider=lambda: [ACCOUNT], max_seconds=seconds)

    def assert_no_runtime(self):
        directory = self.root / 'output/literature-worker/processes'
        self.assertFalse(directory.exists() and list(directory.iterdir()))

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 1
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(alive(pid), '本轮进程未终止')

    def cleanup_receipt_race(self, receipt):
        from highscore_literature.process_runner import _stop_process
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=True)
        release_parent, release_child = context.Pipe(duplex=True)
        process = context.Process(target=cleanup_receipt_after_release,
                                  args=(child, release_child, receipt))
        for connection in (parent, child, release_parent, release_child):
            self.addCleanup(connection.close)
        process.start()
        child.close()
        release_child.close()
        def cleanup():
            if not process._closed:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
                process.close()
        self.addCleanup(cleanup)
        lifecycle = Mock(wraps=parent)
        first = True
        def poll(timeout=0):
            nonlocal first
            if first:
                first = False
                self.assertFalse(parent.poll(0), '首次轮询必须先真实确认管道无回执')
                release_parent.send_bytes(b'go')
                process.join(timeout=3)
                self.assertFalse(process.is_alive(), '必须固定为轮询后 supervisor 已退出')
                return False
            return parent.poll(timeout)
        lifecycle.poll.side_effect = poll
        try:
            _stop_process(process, False, None, time.monotonic()+5, lifecycle)
            return True
        except LeaseLost:
            return False

    def test_clean_receipt_buffered_after_empty_poll_is_read_after_process_exit(self):
        self.assertTrue(self.cleanup_receipt_race(b'clean'), '已入真实管道的 clean 不应误报租约丢失')

    def test_unclean_receipt_buffered_after_process_exit_still_rejects_result(self):
        self.assertFalse(self.cleanup_receipt_race(b'unclean'), 'unclean 仍须禁止回传结果')

    def test_real_engine_runs_outside_parent_and_keeps_worker_mutex(self):
        self.assertTrue(self.worker(ProcessEngine(self.root)).run_once())
        self.assertNotEqual(self.marker['pid'], os.getpid())
        self.assertFalse(self.marker['mutex_acquired'])
        self.assert_stopped(self.marker['pid'])
        self.assert_no_runtime()

    def test_default_engine_also_runs_in_child(self):
        worker = self.worker()
        self.assertIs(type(worker.engine), LiteratureEngine)
        worker.engine.browser_factory = BrowserFactory()
        self.assertTrue(worker.run_once())
        self.assertNotEqual(self.marker['pid'], os.getpid())
        self.assertEqual(self.client.update.call_args.args[2]['state'], 'waiting')
        self.assert_no_runtime()

    def test_noncooperative_engine_timeout_stops_child_and_heartbeats(self):
        started = time.monotonic()
        self.assertTrue(self.worker(ProcessEngine(self.root, 'hang'), seconds=5).run_once())
        self.assertLess(time.monotonic() - started, 7)
        self.assert_stopped(self.marker['pid'])
        values = self.client.update.call_args.args[2]
        self.assertEqual(values['state'], 'waiting')
        self.assertNotIn('pdf_base64', values)
        heartbeats = self.client.heartbeat.call_count
        time.sleep(.12)
        self.assertEqual(self.client.heartbeat.call_count, heartbeats)
        self.assert_no_runtime()

    def test_browser_close_hang_is_included_in_hard_budget(self):
        engine = LiteratureEngine(self.root, browser_factory=BrowserFactory('close_hang'))
        started = time.monotonic()
        self.assertTrue(self.worker(engine, seconds=5).run_once())
        self.assertLess(time.monotonic() - started, 7)
        self.assert_stopped(self.marker['pid'])
        self.assertEqual(self.client.update.call_args.args[2]['state'], 'waiting')

    def test_earlier_job_deadline_shortens_hard_budget(self):
        self.job['deadline'] = time.time() + 5
        started = time.monotonic()
        self.assertTrue(self.worker(ProcessEngine(self.root, 'hang'), seconds=20).run_once())
        self.assertLess(time.monotonic() - started, 7)
        self.assert_stopped(self.marker['pid'])

    def test_large_pdf_result_does_not_wait_for_pipe_buffer_or_exit(self):
        size = 8 * 1024 * 1024
        self.assertTrue(self.worker(ProcessEngine(self.root, size=size), seconds=5).run_once())
        values = self.client.update.call_args.args[2]
        self.assertEqual(values['pdf_base64'], 'A' * size)
        self.assert_no_runtime()

    def test_abnormal_exit_and_private_exception_return_only_fixed_waiting(self):
        for mode in ('exit', 'raise'):
            with self.subTest(mode=mode):
                self.client.update.reset_mock()
                self.assertTrue(self.worker(ProcessEngine(self.root, mode)).run_once())
                values = self.client.update.call_args.args[2]
                self.assertEqual(values['state'], 'waiting')
                text = json.dumps(values) + (self.root / 'output/literature-worker/worker.log').read_text(encoding='utf-8')
                self.assertNotIn('synthetic-private-password', text)
                self.assertNotIn('synthetic-lease', text)
                self.assertNotIn('Traceback', text)
                self.assert_no_runtime()

    def test_lease_loss_kills_only_this_process_tree_without_update(self):
        unrelated = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            unrelated.terminate()
            unrelated.wait(timeout=3)
        self.addCleanup(cleanup)
        def heartbeat(*args):
            if self.marker.get('descendant'):
                raise OSError('private cookie')
            return {'cancel_requested': False}
        self.client.heartbeat.side_effect = heartbeat
        self.assertFalse(self.worker(ProcessEngine(self.root, 'tree')).run_once())
        self.assert_stopped(self.marker['pid'])
        self.assert_stopped(self.marker['descendant'])
        self.assertIsNone(unrelated.poll())
        self.client.update.assert_not_called()
        self.assert_no_runtime()

    def test_completed_result_also_terminates_leftover_descendants(self):
        self.assertTrue(self.worker(ProcessEngine(self.root, 'tree_return')).run_once())
        self.assert_stopped(self.marker['pid'])
        self.assert_stopped(self.marker['descendant'])
        self.client.update.assert_called_once()
        self.assert_no_runtime()

    def cleanup_detached_fixture(self):
        details = self.marker
        pid = details.get('descendant')
        if not pid:
            return
        try:
            current = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            if current == details.get('descendant_starttime'):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass

    @unittest.skipUnless(sys.platform == 'linux', 'Linux 分离 session 回归')
    def test_linux_detached_descendant_is_reaped_after_timeout(self):
        self.addCleanup(self.cleanup_detached_fixture)
        self.assertTrue(self.worker(ProcessEngine(self.root, 'detached_hang'), seconds=5).run_once())
        self.assert_stopped(self.marker['descendant'])
        self.assertFalse(Path(f"/proc/{self.marker['descendant']}").exists(), '分离后代尚未回收')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux 分离 session 回归')
    def test_linux_detached_descendant_is_reaped_after_normal_result(self):
        self.addCleanup(self.cleanup_detached_fixture)
        self.assertTrue(self.worker(ProcessEngine(self.root, 'detached_return')).run_once())
        self.assert_stopped(self.marker['descendant'])
        self.assertFalse(Path(f"/proc/{self.marker['descendant']}").exists(), '分离后代尚未回收')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux 分离 session 回归')
    def test_linux_detached_descendant_is_reaped_after_abrupt_engine_exit(self):
        self.addCleanup(self.cleanup_detached_fixture)
        self.assertTrue(self.worker(ProcessEngine(self.root, 'detached_exit')).run_once())
        self.assert_stopped(self.marker['descendant'])
        self.assertFalse(Path(f"/proc/{self.marker['descendant']}").exists(), '分离后代尚未回收')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux 分离 session 回归')
    def test_linux_detached_descendant_is_reaped_after_intermediate_exit(self):
        self.addCleanup(self.cleanup_detached_fixture)
        self.assertTrue(self.worker(ProcessEngine(self.root, 'detached_intermediate'), seconds=5).run_once())
        self.assert_stopped(self.marker['descendant'])
        self.assertFalse(Path(f"/proc/{self.marker['descendant']}").exists(), '分离后代尚未回收')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux 分离 session 回归')
    def test_linux_detached_descendant_is_reaped_on_cancel_without_update(self):
        self.addCleanup(self.cleanup_detached_fixture)
        self.client.heartbeat.side_effect = lambda *args: {'cancel_requested': bool(self.marker.get('descendant'))}
        self.assertFalse(self.worker(ProcessEngine(self.root, 'detached_cancel')).run_once())
        self.client.update.assert_not_called()
        self.assert_stopped(self.marker['descendant'])
        self.assertFalse(Path(f"/proc/{self.marker['descendant']}").exists(), '分离后代尚未回收')

    def test_cancellation_after_child_result_drops_result(self):
        def heartbeat(*args):
            return {'cancel_requested': bool(self.marker.get('finished'))}
        self.client.heartbeat.side_effect = heartbeat
        self.assertFalse(self.worker(ProcessEngine(self.root, size=1024)).run_once())
        self.client.update.assert_not_called()
        self.assert_stopped(self.marker['pid'])

    def test_publish_intent_survives_timeout_and_next_slice_never_republishes(self):
        engine = LiteratureEngine(self.root, browser_factory=BrowserFactory('publish_hang'))
        self.assertTrue(self.worker(engine, seconds=5).run_once())
        self.assertEqual(StateStore(self.root).read(ACCOUNT['account_ref'], TITLE)['phase'], 'publish_intent')
        next_engine = LiteratureEngine(self.root, browser_factory=BrowserFactory('empty'))
        self.assertTrue(self.worker(next_engine).run_once())
        self.assertEqual(self.client.update.call_args.args[2]['state'], 'needs_attention')
        self.assertEqual((self.root / 'output/literature-worker/publish-count.txt').read_text(encoding='ascii'), '1')
        self.assertEqual(StateStore(self.root).read(ACCOUNT['account_ref'], TITLE)['phase'], 'publish_intent')
        self.assert_no_runtime()

    def test_background_guard_never_starts_heartbeats_after_budget(self):
        calls = []
        def heartbeat(*args):
            calls.append(time.monotonic())
            return {'cancel_requested': False}
        self.client.heartbeat.side_effect = heartbeat
        with LeaseGuard(self.client, self.job, 'synthetic-lease', lease_seconds=.03, max_seconds=.04) as guard:
            time.sleep(.12)
            self.assertTrue(all(instant < guard.until for instant in calls))

    def test_worker_never_expands_production_budget_above_900_seconds(self):
        seen = []
        def work(job, guard):
            seen.append(guard.remaining_seconds())
            return {'state': 'waiting', 'message': '离线预算检查'}
        self.assertTrue(self.worker(work, seconds=1800).run_once())
        self.assertLessEqual(seen[0], 900)
        self.assertGreater(seen[0], 899)

    def test_stuck_heartbeat_cannot_hold_parent_forever(self):
        blocked = threading.Event()
        self.addCleanup(blocked.set)
        self.client.heartbeat.side_effect = lambda *args: blocked.wait(30)
        started = time.monotonic()
        self.assertFalse(self.worker(ProcessEngine(self.root), seconds=.12).run_once())
        self.assertLess(time.monotonic() - started, 1)
        self.client.update.assert_not_called()

    def test_stuck_final_update_has_a_separate_bounded_report_window(self):
        blocked = threading.Event()
        self.addCleanup(blocked.set)
        self.client.update.side_effect = lambda *args: blocked.wait(30)
        started = time.monotonic()
        with patch('highscore_literature.worker.FINAL_SECONDS', .12, create=True):
            self.assertFalse(self.worker(lambda job, guard: {'state': 'waiting'}, seconds=2).run_once())
        self.assertLess(time.monotonic() - started, 1)
        count = self.client.heartbeat.call_count
        time.sleep(.1)
        self.assertEqual(count, self.client.heartbeat.call_count)


if __name__ == '__main__':
    unittest.main()
