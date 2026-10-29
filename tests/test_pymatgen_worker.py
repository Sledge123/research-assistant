"""执行器实际子进程、取消、失联与未知完成重试。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from highscore_pymatgen.worker import (ApiError, _child_environment, _json_file,
                                      deliver, execute, run, stop_process)
from highscore_science.lock import ScienceLock
from test_pymatgen_api import CIF, sample_bundle

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def post(self, route, payload):
        self.calls.append((route, payload))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class CallbackClient:
    def __init__(self, callback):
        self.callback, self.calls = callback, []

    def post(self, route, payload):
        self.calls.append((route, payload))
        return self.callback(len(self.calls), route, payload)


class PymatgenWorkerTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='pymatgen-worker-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {'job_id': 'a' * 32, 'complete': {'lease_token': 'synthetic-lease-token-' * 3,
            'state': 'succeeded', 'bundle_base64': 'synthetic'}}
        _json_file(self.root / 'pending.json', self.payload)

    def lifecycle(self, mode='hang'):
        work, cache = self.root / 'work', self.root / '.cache'
        work.mkdir(exist_ok=True); (cache / 'tmp').mkdir(parents=True, exist_ok=True)
        marker, script = self.root / 'child-marker.json', self.root / 'child.py'
        script.write_text('''import json,os,shutil,sys,time
from pathlib import Path
mode,marker,work,source=sys.argv[1:]
directory=next(next(Path(work).iterdir()).iterdir())
Path(marker).write_text(json.dumps({'pid':os.getpid()}),encoding='utf-8')
if mode=='success':
    shutil.copyfile(source,directory/'result.zip')
elif mode=='invalid':
    (directory/'result.zip').write_bytes(b'half-zip')
elif mode=='hang':
    time.sleep(60)
''', encoding='utf-8')
        source = self.root / 'source.zip'; source.write_bytes(sample_bundle())
        claim = {'job': {'id': 'a' * 32, 'operation': 'convert', 'parameters': {},
            'inputs': [{'filename': '结构.cif', 'content_base64': base64.b64encode(CIF).decode('ascii'),
                        'sha256': hashlib.sha256(CIF).hexdigest()}]}, 'lease_token': 'synthetic-lease-token-' * 3}
        command = [sys.executable, '-B', '-X', 'utf8', str(script), mode, str(marker), str(work), str(source)]
        return claim, work, cache, marker, command

    @staticmethod
    def ready(marker):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                return json.loads(marker.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                time.sleep(.01)
        raise AssertionError('受控子进程未就绪。')

    def execute_tracked(self, client, claim, work, cache, command, **kwargs):
        actual, processes = subprocess.Popen, []
        def tracked(*args, **options):
            process = actual(*args, **options)
            processes.append(process)
            self.addCleanup(lambda: process.poll() is None and stop_process(process))
            return process
        with patch('highscore_pymatgen.worker._critical_memory', return_value=False), \
                patch('highscore_pymatgen.worker.subprocess.Popen', side_effect=tracked):
            result = execute(client, claim, self.root, work, cache, threading.Event(), command=command, **kwargs)
        return result, processes

    def test_unknown_completion_retries_same_payload_and_never_runs_calculation(self):
        client = FakeClient([ApiError(0), {'accepted': True}])
        with self.assertRaises(ApiError):
            deliver(client, self.root, self.payload)
        self.assertTrue((self.root / 'pending.json').exists())
        deliver(client, self.root, self.payload)
        self.assertEqual(client.calls[0], client.calls[1]); self.assertFalse((self.root / 'pending.json').exists())

    def test_rejected_success_resolves_cancel_or_failure_without_recomputing(self):
        for cancelled in (True, False):
            _json_file(self.root / 'pending.json', self.payload)
            client = FakeClient([ApiError(422), {'accepted': True, 'cancel_requested': cancelled}, {'accepted': True}])
            deliver(client, self.root, self.payload)
            self.assertEqual(client.calls[-1][1]['state'], 'cancelled' if cancelled else 'failed')
            self.assertNotIn('bundle_base64', client.calls[-1][1])

    def test_real_cancel_reaps_child_before_releasing_shared_lock(self):
        claim, work, cache, marker, command = self.lifecycle()
        def callback(number, *_):
            if number > 1:
                self.ready(marker)
            return {'accepted': True, 'cancel_requested': number > 1}
        (directory, result), processes = self.execute_tracked(CallbackClient(callback), claim, work, cache, command)
        self.assertEqual(result['state'], 'cancelled'); self.assertIsNotNone(processes[0].poll())
        self.assertFalse((directory / 'result.zip').exists())
        with ScienceLock(self.root) as physical:
            self.assertTrue(physical.acquire())

    def test_timeout_reaps_actual_child_and_invalid_success_is_failed(self):
        for mode in ('hang', 'invalid'):
            claim, work, cache, _, command = self.lifecycle(mode)
            # 每个受控模式独占自己的工作目录，避免夹具脚本选择上一轮目录。
            if mode == 'invalid':
                work = self.root / 'second-work'; work.mkdir()
                command[-2] = str(work)
            (_, result), processes = self.execute_tracked(CallbackClient(lambda *_: {'accepted': True, 'cancel_requested': False}),
                claim, work, cache, command, budget=.1 if mode == 'hang' else 5)
            self.assertEqual(result['state'], 'timed_out' if mode == 'hang' else 'failed')
            self.assertIsNotNone(processes[0].poll()); self.assertNotIn('bundle_base64', result)

    def test_lost_heartbeat_stops_child_and_creates_no_pending_success(self):
        claim, work, cache, marker, command = self.lifecycle()
        def callback(number, *_):
            if number > 1:
                self.ready(marker)
                raise ApiError(0)
            return {'accepted': True, 'cancel_requested': False}
        with self.assertRaises(ApiError):
            self.execute_tracked(CallbackClient(callback), claim, work, cache, command)
        directory = next((work / claim['job']['id']).iterdir())
        self.assertEqual(json.loads((directory / 'outcome.json').read_text())['state'], 'lease_lost')
        self.assertFalse((directory / 'pending.json').exists())
        with ScienceLock(self.root) as physical:
            self.assertTrue(physical.acquire())

    def test_lock_busy_keeps_lease_heartbeat_and_cancels_without_launching(self):
        claim, work, cache, _, command = self.lifecycle()
        with ScienceLock(self.root) as held:
            self.assertTrue(held.acquire())
            client = CallbackClient(lambda number, *_: {'accepted': True, 'cancel_requested': number > 1})
            (_, result), processes = self.execute_tracked(client, claim, work, cache, command)
        self.assertEqual(result['state'], 'cancelled'); self.assertEqual(processes, [])
        self.assertEqual(len(client.calls), 2)

    def test_actual_success_validates_before_return_and_keeps_original_input(self):
        claim, work, cache, _, command = self.lifecycle('success')
        (directory, result), processes = self.execute_tracked(CallbackClient(lambda *_: {'accepted': True, 'cancel_requested': False}),
            claim, work, cache, command, budget=10)
        self.assertEqual(result['state'], 'succeeded'); self.assertIsNotNone(processes[0].poll())
        task = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(task['inputs'], [{'filename': '结构.cif', 'path': 'input-a.cif'}])
        self.assertEqual((directory / 'input-a.cif').read_bytes(), CIF)

    def test_restart_delivers_pending_receipt_before_any_claim(self):
        directory = self.root / 'output/pymatgen-worker' / ('a' * 32) / ('b' * 32)
        _json_file(directory / 'pending.json', self.payload)
        client = FakeClient([{'accepted': True}, {'job': None}])
        with patch('highscore_pymatgen.worker.Client', return_value=client), \
                patch('highscore_pymatgen.worker.signal.signal'), \
                patch('highscore_pymatgen.worker.available', return_value=False), \
                patch('highscore_pymatgen.worker.execute', side_effect=AssertionError('禁止重算')):
            self.assertEqual(run(self.root, once=True), 0)
        self.assertTrue(client.calls[0][0].endswith('/complete')); self.assertEqual(client.calls[1][0], '/claim')

    def test_child_environment_contains_no_role_or_mail_secrets(self):
        with patch.dict(os.environ, {'HIGHSCORE_PYMATGEN_WORKER_TOKEN': 'secret', 'HIGHSCORE_ABTEM_WORKER_TOKEN': 'other',
                                    'LITERATURE_WORKER_TOKEN': 'literature', 'SMTP_PASSWORD': 'mail'}):
            environment = _child_environment(self.root, self.root / '.cache')
        self.assertFalse(any('TOKEN' in key or 'SMTP' in key or 'PASSWORD' in key for key in environment))
        self.assertEqual(environment['OPENBLAS_NUM_THREADS'], '1')
        self.assertTrue(Path(environment['TMPDIR']).is_relative_to(self.root))


if __name__ == '__main__':
    unittest.main()
