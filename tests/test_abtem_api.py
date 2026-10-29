"""电镜模拟真实API、租约、归属、取消和篡改测试，无外部网络。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from highscore_web import create_app
from highscore_web.abtem_store import AbtemError
from highscore_web.credits import Credits
from test_abtem_store import CIF, sample_bundle, upload

ROOT = Path(__file__).resolve().parents[1]
BASE = '/highscore/api/abtem'
WORKER = '/highscore/api/worker/abtem'


class AbtemApiTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='abtem-api-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(self.root, {'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
            'worker_token': 'synthetic-xrd-worker-token-12345678',
            'literature_worker_token': 'synthetic-literature-worker-token-12345678',
            'literature_mail_worker_token': 'synthetic-literature-mail-worker-token-12345678',
            'abtem_worker_token': 'synthetic-abtem-worker-token-12345678', 'abtem_enabled': True})
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.headers = {'X-CSRF-Token': 'synthetic-csrf', 'Origin': 'http://testserver'}
        self.worker = {'Authorization': 'Bearer synthetic-abtem-worker-token-12345678'}
        with self.app.state.database.connection(write=True) as db:
            for identifier in ('u1', 'u2'):
                db.execute('INSERT INTO users VALUES (?,?,\'unused\',1,0,?)',
                           (identifier, identifier + '@example.test', time.time()))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (hashlib.sha256(identifier.encode()).hexdigest(), identifier, 'synthetic-csrf', time.time() + 3600))
        self.client.cookies.set('highscore_session', 'u1')

    def submit(self, key='request-1', **values):
        return self.client.post(BASE + '/jobs', headers=self.headers,
                                json={'request_key': key, 'source': upload(), 'parameters': {}, **values})

    def claim(self, worker='synthetic-abtem'):
        response = self.client.post(WORKER + '/claim', headers=self.worker,
                                    json={'worker_id': worker, 'available': True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def complete(self, claim, state='succeeded', **values):
        if state == 'succeeded' and 'bundle_base64' not in values:
            values['bundle_base64'] = base64.b64encode(sample_bundle()).decode('ascii')
        return self.client.post(WORKER + f"/jobs/{claim['job']['id']}/complete", headers=self.worker,
            json={'lease_token': claim['lease_token'], 'state': state, **values})

    def wallet(self):
        return self.client.get('/highscore/api/credits').json()

    def test_success_flat_download_images_and_repeated_receipts_charge_once(self):
        submitted = self.submit(); self.assertEqual(submitted.status_code, 200, submitted.text)
        job = submitted.json(); claim = self.claim()
        for _ in range(2):
            response = self.complete(claim); self.assertEqual(response.status_code, 200, response.text)
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(final['state'], 'succeeded')
        self.assertTrue(final['download_available'])
        self.assertEqual(final['credits'], {'reserved': 1, 'charged': 1, 'status': 'charged'})
        self.assertEqual(final['result_summary']['atom_count'], 1)
        for _ in range(2):
            response = self.client.get(BASE + '/jobs/' + job['id'] + '/download')
            self.assertEqual(response.status_code, 200, response.text)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(len(archive.namelist()), 13)
                self.assertTrue(all('/' not in item for item in archive.namelist()))
                self.assertEqual(archive.read('input.cif'), CIF)
        for kind in ('saed', 'hrtem'):
            image = self.client.get(BASE + '/jobs/' + job['id'] + '/image/' + kind)
            self.assertEqual(image.status_code, 200, image.text)
            self.assertTrue(image.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 0))
        charges = [item for item in self.wallet()['items'] if item['kind'] == 'abtem_charge']
        self.assertEqual(len(charges), 1); self.assertEqual(charges[0]['job_kind'], 'abtem')
        self.assertEqual(self.wallet()['items'][0]['label'], '电镜模拟结果扣分')

    def test_idempotency_binds_original_input_parameters_and_active_owner(self):
        first = self.submit().json()
        self.assertEqual(self.submit().json()['id'], first['id'])
        self.assertEqual(self.submit(source=upload(CIF + b'# changed\n')).status_code, 409)
        self.assertEqual(self.submit(source=upload(b'changed bytes without CIF header')).status_code, 409)
        self.assertEqual(self.submit(source={**upload(), 'filename': 'another.cif'}).status_code, 409)
        self.assertEqual(self.submit(parameters={'energy_kev': 100}).status_code, 409)
        self.assertEqual(self.submit('different-key').status_code, 409)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 1))

    def test_bad_inputs_and_unknown_paths_do_not_freeze_credits(self):
        bad = (
            {'source': {**upload(), 'filename': '../input.cif'}},
            {'source': {**upload(), 'filename': 'D:\\private\\input.cif'}},
            {'source': {**upload(), 'cif_base64': 'invalid*'}},
            {'source': upload(b'not a cif')},
            {'source': {**upload(), 'path': 'input.cif'}},
            {'parameters': {'energy_kev': True}},
            {'parameters': {'energy_kev': 301}},
            {'parameters': {'zone_axis': [0, 0, 0]}},
            {'parameters': {'grid': 4096}},
        )
        for values in bad:
            with self.subTest(values=values):
                self.assertEqual(self.submit(**values).status_code, 422)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))

    def test_verified_session_origin_csrf_and_independent_worker_token(self):
        self.assertEqual(self.client.post(BASE + '/jobs', json={'request_key': 'key', 'source': upload(), 'parameters': {}}).status_code, 403)
        self.assertEqual(self.client.post(BASE + '/jobs', headers={**self.headers, 'Origin': 'https://evil.invalid'}, json={'request_key': 'key', 'source': upload(), 'parameters': {}}).status_code, 403)
        for token in ('synthetic-xrd-worker-token-12345678', 'synthetic-literature-worker-token-12345678', 'synthetic-literature-mail-worker-token-12345678'):
            reply = self.client.post(WORKER + '/claim', headers={'Authorization': 'Bearer ' + token}, json={'worker_id': 'bad-worker', 'available': True})
            self.assertEqual(reply.status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(BASE + '/status').status_code, 401)
        self.assertEqual(self.client.get(BASE + '/jobs').status_code, 401)
        self.client.cookies.set('highscore_session', 'u1')
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET verified=0 WHERE id='u1'")
        self.assertEqual(self.submit().status_code, 401)

    def test_global_claim_is_atomic_and_other_user_cannot_view_or_cancel(self):
        first = self.submit().json()
        self.client.cookies.set('highscore_session', 'u2')
        second = self.submit().json()
        for suffix in ('', '/download', '/image/saed'):
            self.assertEqual(self.client.get(BASE + '/jobs/' + first['id'] + suffix).status_code, 404)
        self.assertEqual(self.client.post(BASE + '/jobs/' + first['id'] + '/cancel', headers=self.headers, json={}).status_code, 404)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(self.claim, ['worker-' + str(index) for index in range(4)]))
        taken = [claim for claim in claims if claim['job']]
        self.assertEqual(len(taken), 1)
        self.assertIn(taken[0]['job']['id'], (first['id'], second['id']))
        self.assertEqual(self.complete(taken[0], 'failed').status_code, 200)
        self.assertIsNotNone(self.claim()['job'])

    def test_cancel_queued_refunds_once_and_running_waits_for_worker_confirmation(self):
        job = self.submit().json()
        url = BASE + '/jobs/' + job['id'] + '/cancel'
        for _ in range(2):
            reply = self.client.post(url, headers=self.headers, json={})
            self.assertEqual(reply.json()['state'], 'cancelled')
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))
        active = self.submit('request-2').json(); claim = self.claim()
        url = BASE + '/jobs/' + active['id'] + '/cancel'
        cancelled = self.client.post(url, headers=self.headers, json={}).json()
        self.assertEqual(cancelled['state'], 'running'); self.assertTrue(cancelled['cancel_requested'])
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 1))
        beat = self.client.post(WORKER + '/jobs/' + active['id'] + '/heartbeat', headers=self.worker,
            json={'lease_token': claim['lease_token'], 'progress': 25, 'phase': 'calculating'}).json()
        self.assertTrue(beat['cancel_requested'])
        self.assertEqual(self.complete(claim).status_code, 409)
        self.assertFalse((self.root / 'output/abtem' / active['id']).exists())
        for _ in range(2):
            self.assertEqual(self.complete(claim, 'cancelled').status_code, 200)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))

    def test_wrong_and_expired_lease_cannot_publish_and_timeout_refunds_once(self):
        job = self.submit().json(); claim = self.claim()
        wrong = {**claim, 'lease_token': 'wrong-token-' * 4}
        self.assertEqual(self.complete(wrong).status_code, 409)
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE abtem_jobs SET lease_until=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertEqual(self.complete(claim).status_code, 409)
        self.assertEqual(self.complete(claim).status_code, 409)
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(final['state'], 'timed_out'); self.assertEqual(final['credits']['status'], 'refunded')
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))
        self.assertFalse((self.root / 'output/abtem' / job['id']).exists())

    def test_tampered_worker_bundle_and_saved_file_are_not_downloadable(self):
        job = self.submit().json(); claim = self.claim()
        source = zipfile.ZipFile(io.BytesIO(sample_bundle()))
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
            for item in source.namelist():
                value = source.read(item)
                archive.writestr(item, value + b'tamper' if item == 'saed.png' else value)
        response = self.complete(claim, bundle_base64=base64.b64encode(content.getvalue()).decode('ascii'))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 1))
        self.assertEqual(self.complete(claim).status_code, 200)
        path = self.root / 'output/abtem' / job['id'] / 'result.zip'
        path.write_bytes(path.read_bytes() + b'tamper')
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 410)
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/image/hrtem').status_code, 410)
        self.assertFalse(self.client.get(BASE + '/jobs/' + job['id']).json()['download_available'])
        self.assertEqual(self.wallet()['available'], 9)

    def test_successful_result_expiry_deletes_bundle_and_input_but_preserves_ledger(self):
        job = self.submit().json(); claim = self.claim(); self.complete(claim)
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE abtem_jobs SET download_expires_at=? WHERE id=?', (time.time() - 1, job['id']))
        response = self.client.get(BASE + '/jobs/' + job['id'])
        self.assertEqual(response.json()['state'], 'succeeded'); self.assertTrue(response.json()['file_expired'])
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 410)
        with self.app.state.database.connection() as db:
            row = db.execute('SELECT * FROM abtem_jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertIsNone(row['input_cif']); self.assertIsNone(row['artifact_path'])
            self.assertTrue(row['parameters']); self.assertTrue(row['result_summary'])
            self.assertEqual(row['credit_status'], 'charged')
        self.assertFalse((self.root / 'output/abtem' / job['id']).exists())
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 0))

    def test_slow_validation_does_not_lock_credits_and_cancel_wins(self):
        job = self.submit().json(); claim = self.claim()
        encoded = base64.b64encode(sample_bundle()).decode('ascii')
        from highscore_abtem.artifacts import validate_bundle
        entered, resume = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(10))
            return validate_bundle(*args, **kwargs)

        with patch('highscore_abtem.artifacts.validate_bundle', side_effect=slow):
            with ThreadPoolExecutor(max_workers=2) as pool:
                completed = pool.submit(self.complete, claim, bundle_base64=encoded)
                self.assertTrue(entered.wait(10))
                cancelled = self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers, json={})
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(self.client.post('/highscore/api/credits/checkin', headers=self.headers, json={}).status_code, 200)
                resume.set()
                self.assertEqual(completed.result().status_code, 409)
        self.assertFalse((self.root / 'output/abtem' / job['id']).exists())
        self.assertEqual(self.complete(claim, 'cancelled').status_code, 200)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (11, 0))

    def test_failed_scientific_input_has_explicit_safe_message_and_refunds(self):
        job = self.submit().json(); claim = self.claim()
        response = self.complete(claim, 'failed', error_code='unsupported_occupancy',
            message='internal /private/synthetic.cif token=synthetic-secret diagnostic')
        self.assertEqual(response.status_code, 200, response.text)
        public = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertIn('部分占有', public['message']); self.assertIn('退回', public['message'])
        self.assertNotIn('private', json.dumps(public)); self.assertNotIn('synthetic-secret', json.dumps(public))
        self.assertEqual(self.complete(claim, 'failed').status_code, 200)
        with self.app.state.database.connection() as db:
            audit = db.execute("SELECT detail FROM audit WHERE action='abtem_settled'").fetchone()[0]
            self.assertIn('synthetic.cif', audit)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))

    def test_queued_timeout_and_fixed_run_deadline_are_not_extended_by_heartbeat(self):
        job = self.submit().json()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE abtem_jobs SET queue_deadline=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id']).json()['state'], 'timed_out')
        job = self.submit('request-2').json(); claim = self.claim()
        with self.app.state.database.connection() as db:
            initial = db.execute('SELECT run_deadline FROM abtem_jobs WHERE id=?', (job['id'],)).fetchone()[0]
        for _ in range(2):
            response = self.client.post(WORKER + '/jobs/' + job['id'] + '/heartbeat', headers=self.worker,
                json={'lease_token': claim['lease_token'], 'progress': 25, 'phase': 'calculating'})
            self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.database.connection() as db:
            final = db.execute('SELECT lease_until,run_deadline FROM abtem_jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(final['run_deadline'], initial); self.assertLessEqual(final['lease_until'], initial)
        self.complete(claim, 'timed_out')
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))

    def test_duplicate_concurrent_success_and_changed_success_cannot_double_charge(self):
        job = self.submit().json(); claim = self.claim()
        encoded = base64.b64encode(sample_bundle()).decode('ascii')
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.complete(claim, bundle_base64=encoded), range(2)))
        self.assertTrue(all(response.status_code == 200 for response in replies), [response.text for response in replies])
        changed = base64.b64encode(sample_bundle(factor=2)).decode('ascii')
        self.assertEqual(self.complete(claim, bundle_base64=changed).status_code, 409)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 0))
        self.assertEqual(len([item for item in self.wallet()['items'] if item['kind'] == 'abtem_charge']), 1)

    def test_feature_gate_is_independent_and_offline_worker_does_not_claim(self):
        self.app.state.abtem.enabled = False
        self.assertEqual(self.submit().status_code, 503)
        self.assertFalse(self.client.get(BASE + '/status').json()['enabled'])
        self.app.state.abtem.enabled = True
        self.submit()
        response = self.client.post(WORKER + '/claim', headers=self.worker,
                                    json={'worker_id': 'unavailable', 'available': False})
        self.assertIsNone(response.json()['job'])
        self.assertFalse(self.client.get(BASE + '/status').json()['worker_online'])
        self.assertIsNotNone(self.claim()['job'])
        status = self.client.get(BASE + '/status').json()
        self.assertTrue(status['worker_online']); self.assertEqual(status['message'], '')
        self.assertEqual(status['limits']['energy_kev'], [80, 300])

    def test_symlink_result_ancestor_rejected_before_publish(self):
        job = self.submit().json(); claim = self.claim()
        original = Path.is_symlink
        def symlink(path):
            return path == self.root / 'output/abtem' or original(path)
        with patch.object(Path, 'is_symlink', symlink):
            response = self.complete(claim)
            self.assertEqual(response.status_code, 410)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 1))
        self.assertFalse((self.root / 'output/abtem' / job['id']).exists())

    def test_only_exact_abtem_routes_have_larger_request_limits(self):
        oversized = {'request_key': 'request-1', 'source': {**upload(), 'cif_base64': 'x' * 740000}, 'parameters': {}}
        self.assertEqual(self.client.post(BASE + '/jobs', headers=self.headers, json=oversized).status_code, 413)
        response = self.client.post(BASE + '/jobs/not-real/cancel', headers=self.headers, content=b' ' * 21000)
        self.assertEqual(response.status_code, 413)
        response = self.client.post(WORKER + '/jobs/not-real/complete', headers=self.worker, content=b' ' * 21000)
        self.assertEqual(response.status_code, 413)

    def test_archive_input_rechecks_receipts_ownership_and_tampered_cif(self):
        from highscore_database.exporter import DatabaseExporter
        from highscore_database.executor import package_archive
        from highscore_database.jade_structure import FORMAT, JadeStructureReader
        from highscore_database.jade_structure_build import build_database
        from highscore_web.artifacts import accept_zip
        from highscore_web.database import dumps
        from test_jade_structure_reader import fixture_source
        source = fixture_source(self.root / 'source')
        database = self.root / 'structure.sqlite3'; built = build_database(source, database)
        reader = JadeStructureReader(database, profile={'format': FORMAT, 'source': 'ICSD（测试结构）', 'sha256': built['sha256']})
        producer = self.root / 'producer'; producer.mkdir()
        receipt = DatabaseExporter(producer, reader).export('ICSD-000001')
        encoded = package_archive(producer, [{'status': 'success', 'receipt_path': receipt['receipt_path']}])
        relative, _ = accept_zip(self.root, 'synthetic-archive', encoded, [{'code': 'ICSD-000001'}])
        with self.app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,archive_status,artifact_dir,created_at,updated_at) VALUES ('synthetic-archive','u1','archive','succeeded',?,'synthetic','archive','success',?,?,?)",
                       (dumps({'codes': 'ICSD-000001'}), relative, time.time(), time.time()))
        listed = self.client.get(BASE + '/sources').json()['items']
        self.assertEqual([(item['job_id'], item['code']) for item in listed], [('synthetic-archive', 'ICSD-000001')])
        archive_source = {'type': 'archive', 'job_id': 'synthetic-archive', 'code': 'ICSD-000001'}
        submitted = self.submit(source=archive_source)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.client.post(BASE + '/jobs/' + submitted.json()['id'] + '/cancel', headers=self.headers, json={})
        self.client.cookies.set('highscore_session', 'u2')
        self.assertEqual(self.client.get(BASE + '/sources').json()['items'], [])
        self.assertEqual(self.submit(source=archive_source).status_code, 404)
        self.client.cookies.set('highscore_session', 'u1')
        cif_path = self.root / relative / receipt['files']['cif']['path']
        cif_path.write_bytes(cif_path.read_bytes() + b'\n# changed\n')
        replayed = self.submit(source=archive_source)
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()['id'], submitted.json()['id'])
        self.assertEqual(replayed.json()['state'], 'cancelled')
        self.assertEqual(self.submit(parameters={'energy_kev': 100}, source=archive_source).status_code, 409)
        self.assertEqual(self.submit(source={**archive_source, 'job_id': 'different-archive'}).status_code, 409)
        self.assertEqual(self.submit('request-2', source=archive_source).status_code, 409)
        self.assertEqual(self.client.get(BASE + '/sources').json()['items'], [])


class AbtemBodyLimitTests(unittest.TestCase):
    def test_declared_and_chunked_large_bodies_only_reach_exact_abtem_routes(self):
        import test_admin_body_limits
        invoke = test_admin_body_limits.AdminBodyLimitTests.invoke
        for declared in (False, True):
            for path in (BASE + '/jobs', WORKER + '/jobs/' + 'a' * 32 + '/complete'):
                status, received = invoke(self, path, b' ' * 600000, declared=declared)
                self.assertEqual(status, 204); self.assertEqual(len(received[0]), 600000)
            for path in (BASE + '/jobs-extra', BASE + '/jobs/id/cancel',
                         WORKER + '/jobs/' + 'a' * 32 + '/heartbeat',
                         WORKER + '/jobs/' + 'a' * 32 + '/complete-extra'):
                self.assertEqual(invoke(self, path, b' ' * 20001, declared=declared), (413, []))
            self.assertEqual(invoke(self, BASE + '/jobs', b' ' * (720 * 1024 + 1), declared=declared), (413, []))


if __name__ == '__main__':
    unittest.main()
