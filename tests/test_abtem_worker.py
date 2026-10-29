"""执行器完成回执、终止与凭据隔离，不连接网络。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from highscore_abtem.worker import (ApiError, _child_environment, _json_file,
                                    cleanup, deliver, execute, run, stop_process)
from test_abtem_store import CIF, sample_bundle, upload

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, route, payload):
        self.calls.append((route, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CallbackClient:
    def __init__(self, callback):
        self.callback, self.calls = callback, []

    def post(self, route, payload):
        self.calls.append((route, payload))
        return self.callback(len(self.calls), route, payload)


class LocalApiClient:
    """只适配进程内 ASGI 测试站点，没有网络连接。"""
    def __init__(self, http, *, unknown_once=False, before_complete=None):
        self.http, self.unknown_once = http, unknown_once
        self.before_complete, self.calls = before_complete, []

    def post(self, route, payload):
        self.calls.append((route, payload))
        if route.endswith('/complete') and self.before_complete:
            self.before_complete()
        reply = self.http.post('/highscore/api/worker/abtem' + route,
            headers={'Authorization': 'Bearer synthetic-abtem-worker-token-12345678'}, json=payload)
        if reply.status_code >= 400:
            raise ApiError(reply.status_code)
        if route.endswith('/complete') and self.unknown_once:
            self.unknown_once = False
            # 服务器已经接受，但执行器未收到响应的受控断线。
            raise ApiError(0)
        return reply.json()


class AbtemWorkerTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tmp'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=cache, prefix='abtem-worker-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {'job_id': 'a' * 32, 'complete': {'lease_token': 'synthetic-lease-token-' * 3,
                                                      'state': 'succeeded', 'bundle_base64': 'synthetic'}}
        _json_file(self.root / 'pending.json', self.payload)

    def test_unknown_http_result_retains_exact_payload_then_retries_only_completion(self):
        client = FakeClient([ApiError(0), {'accepted': True}])
        with self.assertRaises(ApiError):
            deliver(client, self.root, self.payload)
        self.assertEqual(json.loads((self.root / 'pending.json').read_text(encoding='utf-8')), self.payload)
        deliver(client, self.root, self.payload)
        self.assertFalse((self.root / 'pending.json').exists())
        self.assertEqual(client.calls[0], client.calls[1])

    def test_cancellation_after_finished_compute_sends_only_cancelled_confirmation(self):
        client = FakeClient([ApiError(409), {'accepted': True, 'cancel_requested': True}, {'accepted': True}])
        deliver(client, self.root, self.payload)
        self.assertEqual(client.calls[-1][1]['state'], 'cancelled')
        self.assertNotIn('bundle_base64', client.calls[-1][1])

    def test_permanent_result_rejection_ends_as_failed_and_does_not_recompute(self):
        client = FakeClient([ApiError(422), {'accepted': True, 'cancel_requested': False}, {'accepted': True}])
        deliver(client, self.root, self.payload)
        self.assertEqual(client.calls[-1][1]['state'], 'failed')
        self.assertEqual(client.calls[-1][1]['error_code'], 'compute')

    def test_closed_lease_receipt_does_not_retry_or_accept_success(self):
        client = FakeClient([ApiError(409), ApiError(409)])
        deliver(client, self.root, self.payload)
        result = json.loads((self.root / 'delivery.json').read_text(encoding='utf-8'))
        self.assertFalse(result['accepted'])
        self.assertFalse((self.root / 'pending.json').exists())

    def test_child_environment_excludes_service_and_proxy_credentials(self):
        with patch.dict(os.environ, {'HIGHSCORE_ABTEM_WORKER_TOKEN': 'synthetic-secret',
                                     'HTTP_PROXY': 'https://synthetic-secret@example.test'}):
            environment = _child_environment(self.root, self.root / '.cache')
        self.assertNotIn('HIGHSCORE_ABTEM_WORKER_TOKEN', environment)
        self.assertNotIn('HTTP_PROXY', environment)
        self.assertEqual(environment['NUMBA_NUM_THREADS'], '1')

    def test_stopping_actual_child_returns_with_process_reaped(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(60)'],
                                 cwd=self.root, start_new_session=(os.name == 'posix'),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        stop_process(child)
        self.assertIsNotNone(child.returncode)

    def test_old_interrupted_attempt_input_cleaned_and_pending_retained(self):
        work = self.root / 'work'
        old = work / ('a' * 32) / ('b' * 32)
        unknown = work / ('c' * 32) / ('d' * 32)
        old.mkdir(parents=True); unknown.mkdir(parents=True)
        (old / 'input.cif').write_text('synthetic', encoding='utf-8')
        (unknown / 'pending.json').write_text('{}', encoding='utf-8')
        os.utime(old, (0, 0)); os.utime(unknown, (0, 0))
        cleanup(work)
        self.assertFalse(old.exists())
        self.assertTrue(unknown.exists())

    def lifecycle(self, mode='hang'):
        work, cache = self.root / 'work', self.root / '.cache'
        work.mkdir(exist_ok=True); (cache / 'tmp').mkdir(parents=True, exist_ok=True)
        marker = self.root / 'child-marker.json'
        script = self.root / 'child.py'
        script.write_text('''import json,os,shutil,sys,time
from pathlib import Path
mode,marker,work,source=sys.argv[1:]
directory=next(next(Path(work).iterdir()).iterdir())
Path(marker).write_text(json.dumps({'pid':os.getpid()}),encoding='utf-8')
if mode=='success':
    shutil.copyfile(source,directory/'result.zip')
elif mode=='invalid':
    (directory/'result.zip').write_bytes(b'not a valid scientific result')
elif mode=='failed':
    (directory/'error.json').write_text(json.dumps({'code':'unsupported_occupancy','message':'controlled occupancy refusal'}),encoding='utf-8')
    raise SystemExit(2)
elif mode=='hang':
    time.sleep(60)
''', encoding='utf-8')
        source = self.root / 'source-result.zip'; source.write_bytes(sample_bundle())
        claim = {'job': {'id': 'a' * 32, 'parameters': {},
                         'cif_base64': base64.b64encode(CIF).decode('ascii'),
                         'input_sha256': hashlib.sha256(CIF).hexdigest(), 'input_name': 'synthetic.cif'},
                 'lease_token': 'synthetic-lease-token-' * 3}
        command = [sys.executable, '-B', '-X', 'utf8', str(script), mode, str(marker), str(work), str(source)]
        return claim, work, cache, marker, command

    @staticmethod
    def ready(marker):
        deadline = time.monotonic() + 5
        while True:
            try:
                return json.loads(marker.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise AssertionError('受控子进程未就绪。')
            time.sleep(.01)

    def execute_tracked(self, client, claim, work, cache, command, stopping=None, **kwargs):
        actual = subprocess.Popen
        processes = []

        def tracked(*args, **options):
            process = actual(*args, **options)
            processes.append(process)
            self.addCleanup(lambda: process.poll() is None and stop_process(process))
            return process

        with patch('highscore_abtem.worker._critical_memory', return_value=False), \
                patch('highscore_abtem.worker.subprocess.Popen', side_effect=tracked):
            result = execute(client, claim, self.root, work, cache, stopping or threading.Event(),
                             command=command, **kwargs)
        return result, processes

    def test_running_cancel_stops_and_reaps_actual_compute_before_returning_cancelled(self):
        claim, work, cache, marker, command = self.lifecycle()
        def callback(number, *_):
            if number > 1:
                self.ready(marker)
            return {'accepted': True, 'cancel_requested': number > 1}
        client = CallbackClient(callback)
        (directory, result), processes = self.execute_tracked(client, claim, work, cache, command)
        self.assertEqual(result['state'], 'cancelled')
        self.assertNotIn('bundle_base64', result)
        self.assertEqual(len(processes), 1); self.assertIsNotNone(processes[0].poll())
        self.assertFalse((directory / 'result.zip').exists())

    def test_initial_cancellation_does_not_launch_compute(self):
        claim, work, cache, _, command = self.lifecycle()
        client = CallbackClient(lambda *_: {'accepted': True, 'cancel_requested': True})
        (_, result), processes = self.execute_tracked(client, claim, work, cache, command)
        self.assertEqual(result['state'], 'cancelled'); self.assertEqual(processes, [])

    def test_timeout_budget_kills_actual_child_and_has_no_success_bundle(self):
        claim, work, cache, marker, command = self.lifecycle()
        def callback(number, *_):
            if number > 1:
                self.ready(marker)
            return {'accepted': True, 'cancel_requested': False}
        started = time.monotonic()
        (_, result), processes = self.execute_tracked(CallbackClient(callback), claim, work, cache, command, budget=.1)
        self.assertEqual(result['state'], 'timed_out')
        self.assertNotIn('bundle_base64', result); self.assertIsNotNone(processes[0].poll())
        self.assertLess(time.monotonic() - started, 5)

    def test_lost_running_heartbeat_kills_actual_child_and_never_submits_success(self):
        claim, work, cache, marker, command = self.lifecycle()
        processes = []; actual = subprocess.Popen
        def tracked(*args, **options):
            process = actual(*args, **options); processes.append(process)
            self.addCleanup(lambda: process.poll() is None and stop_process(process))
            return process
        def callback(number, *_):
            if number > 1:
                self.ready(marker)
                raise ApiError(0)
            return {'accepted': True, 'cancel_requested': False}
        client = CallbackClient(callback)
        with patch('highscore_abtem.worker._critical_memory', return_value=False), \
                patch('highscore_abtem.worker.subprocess.Popen', side_effect=tracked):
            with self.assertRaises(ApiError):
                execute(client, claim, self.root, work, cache, threading.Event(), command=command)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(all(route.endswith('/heartbeat') for route, _ in client.calls))
        attempts = list((work / claim['job']['id']).iterdir())
        self.assertFalse((attempts[0] / 'pending.json').exists())
        self.assertEqual(json.loads((attempts[0] / 'outcome.json').read_text(encoding='utf-8'))['state'], 'lease_lost')

    def test_successful_process_with_invalid_bundle_becomes_failed_completion(self):
        claim, work, cache, _, command = self.lifecycle('invalid')
        client = CallbackClient(lambda *_: {'accepted': True, 'cancel_requested': False})
        (_, result), processes = self.execute_tracked(client, claim, work, cache, command)
        self.assertEqual(result['state'], 'failed'); self.assertEqual(result['error_code'], 'compute')
        self.assertIsNotNone(processes[0].poll())

    def test_unaccepted_heartbeat_never_launches_or_accepts_success(self):
        claim, work, cache, _, command = self.lifecycle('success')
        client = CallbackClient(lambda *_: {'accepted': False, 'cancel_requested': False})
        with self.assertRaises(ApiError):
            self.execute_tracked(client, claim, work, cache, command)

    def test_rejected_bundle_probe_requires_accepted_heartbeat_before_any_fallback(self):
        client = FakeClient([ApiError(422), {'accepted': False, 'cancel_requested': False}])
        with self.assertRaises(ApiError):
            deliver(client, self.root, self.payload)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue((self.root / 'pending.json').exists())

    def test_restart_replays_pending_completion_before_claim_without_recomputing(self):
        attempt = self.root / 'output/abtem-worker' / ('a' * 32) / ('b' * 32)
        attempt.mkdir(parents=True)
        _json_file(attempt / 'pending.json', self.payload)
        client = FakeClient([{'accepted': True}, {'job': None}])
        with patch('highscore_abtem.worker.Client', return_value=client), \
                patch('highscore_abtem.worker.signal.signal'), \
                patch('highscore_abtem.worker.available', return_value=False), \
                patch('highscore_abtem.worker.execute', side_effect=AssertionError('禁止重复计算')):
            self.assertEqual(run(self.root, once=True), 0)
        self.assertEqual([route for route, _ in client.calls], ['/jobs/' + 'a' * 32 + '/complete', '/claim'])
        self.assertEqual(client.calls[0][1], self.payload['complete'])
        self.assertFalse((attempt / 'pending.json').exists())

    @unittest.skipUnless(sys.platform == 'linux', '同组后代回收须在Linux执行器核验')
    def test_stop_reaps_live_process_group_even_when_leader_already_exited(self):
        marker = self.root / 'descendant.json'
        descendant = '''import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'start':Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]}))
time.sleep(60)
'''
        leader = '''import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
while not Path(sys.argv[2]).exists():time.sleep(.01)
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', leader, descendant, str(marker)],
            cwd=self.root, start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and stop_process(process))
        details = self.ready(marker)
        process.wait(timeout=5)
        child = Path('/proc') / str(details['pid']) / 'stat'
        def live():
            try:
                fields = child.read_text().rsplit(')', 1)[1].split()
                return fields[19] == details['start'] and fields[0] not in ('Z', 'X')
            except FileNotFoundError:
                return False
        def cleanup_child():
            if live():
                os.kill(details['pid'], signal.SIGKILL)
        self.addCleanup(cleanup_child)
        self.assertTrue(live())
        stop_process(process)
        deadline = time.monotonic() + 2
        while live() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(live())

    def local_api(self):
        from fastapi.testclient import TestClient
        from highscore_web import create_app
        app = create_app(self.root, {'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
            'abtem_worker_token': 'synthetic-abtem-worker-token-12345678', 'abtem_enabled': True})
        http = TestClient(app); self.addCleanup(http.close)
        with app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u1','synthetic@example.test','unused',1,0,?)", (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                (hashlib.sha256(b'synthetic-session').hexdigest(), 'u1', 'synthetic-csrf', time.time() + 3600))
        http.cookies.set('highscore_session', 'synthetic-session')
        headers = {'X-CSRF-Token': 'synthetic-csrf'}
        reply = http.post('/highscore/api/abtem/jobs', headers=headers,
                         json={'request_key': 'controlled-worker', 'source': upload(), 'parameters': {}})
        self.assertEqual(reply.status_code, 200, reply.text)
        return app, http, headers, reply.json()

    def test_unknown_real_api_completion_retries_exact_result_and_charges_once(self):
        _, work, cache, _, command = self.lifecycle('success')
        app, http, _, _ = self.local_api()
        client = LocalApiClient(http)
        claim = client.post('/claim', {'worker_id': 'synthetic-worker', 'available': True})
        (directory, result), processes = self.execute_tracked(client, claim, work, cache, command)
        self.assertEqual(result['state'], 'succeeded'); self.assertIsNotNone(processes[0].poll())
        payload = {'job_id': claim['job']['id'], 'complete': {'lease_token': claim['lease_token'], **result}}
        _json_file(directory / 'pending.json', payload)
        client.unknown_once = True
        with self.assertRaises(ApiError):
            deliver(client, directory, payload)
        self.assertTrue((directory / 'pending.json').exists())
        deliver(client, directory, payload)
        completions = [(route, body) for route, body in client.calls if route.endswith('/complete')]
        self.assertEqual(len(completions), 2); self.assertEqual(completions[0], completions[1])
        wallet = http.get('/highscore/api/credits').json()
        self.assertEqual((wallet['available'], wallet['frozen']), (9, 0))
        self.assertEqual(len([item for item in wallet['items'] if item['kind'] == 'abtem_charge']), 1)
        with app.state.database.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abtem_jobs').fetchone()[0], 1)

    def test_running_cancel_real_api_refunds_only_after_actual_compute_has_exited(self):
        _, work, cache, marker, command = self.lifecycle()
        _, http, headers, job = self.local_api()
        api = LocalApiClient(http)
        claim = api.post('/claim', {'worker_id': 'synthetic-worker', 'available': True})
        def callback(number, route, payload):
            if number > 1:
                self.ready(marker)
                response = http.post('/highscore/api/abtem/jobs/' + job['id'] + '/cancel', headers=headers, json={})
                self.assertEqual(response.status_code, 200)
                wallet = http.get('/highscore/api/credits').json()
                self.assertEqual((wallet['available'], wallet['frozen']), (9, 1))
            return api.post(route, payload)
        (directory, result), processes = self.execute_tracked(CallbackClient(callback), claim, work, cache, command)
        self.assertEqual(result['state'], 'cancelled'); self.assertIsNotNone(processes[0].poll())
        api.before_complete = lambda: self.assertIsNotNone(processes[0].poll())
        payload = {'job_id': job['id'], 'complete': {'lease_token': claim['lease_token'], **result}}
        _json_file(directory / 'pending.json', payload)
        deliver(api, directory, payload)
        wallet = http.get('/highscore/api/credits').json()
        self.assertEqual((wallet['available'], wallet['frozen']), (10, 0))
        self.assertEqual(http.get('/highscore/api/abtem/jobs/' + job['id']).json()['state'], 'cancelled')
