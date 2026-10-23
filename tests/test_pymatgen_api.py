"""收费材料任务、真实ASGI权限和文件发布；不替代线上网页验收。"""
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
from test_pymatgen_contract import CIF

ROOT = Path(__file__).resolve().parents[1]
BASE = '/highscore/api/pymatgen'
WORKER = '/highscore/api/worker/pymatgen'


def upload(content=CIF, filename='结构.cif'):
    return {'type': 'upload', 'filename': filename, 'content_base64': base64.b64encode(content).decode('ascii')}


def sample_bundle(content=CIF, filename='结构.cif', *, seconds=.1):
    from highscore_pymatgen.artifacts import build_bundle, make_summary, report_text, structure_files
    from highscore_pymatgen.contract import PROFILE_VERSION, PYMATGEN_CORE_VERSION, PYMATGEN_VERSION
    model = {'name': 'structure', 'lattice_matrix_angstrom': [[5.43, 0, 0], [0, 5.43, 0], [0, 0, 5.43]],
             'lattice_parameters': [5.43, 5.43, 5.43, 90., 90., 90.], 'volume_angstrom3': 5.43 ** 3,
             'symbols': ['Si'], 'fractional_positions': [[0., 0., 0.]], 'atom_count': 1,
             'composition': {'Si': 1}, 'formula': 'Si', 'occupancy': 'full', 'source': {}}
    details = {'roundtrip_verified': True}
    result = {'profile_version': PROFILE_VERSION, 'operation': 'convert', 'parameters': {},
              'input_structures': [model], 'structures': [model], 'summary': make_summary('convert', [model], details),
              'details': details, 'versions': {'pymatgen': PYMATGEN_VERSION, 'pymatgen-core': PYMATGEN_CORE_VERSION},
              'runtime': {'seconds': seconds}, 'methods': {}}
    files = structure_files(model)
    files['报告.txt'] = report_text(result)
    return build_bundle([{'filename': filename, 'content': content}], 'convert', {}, result, files)


class PymatgenApiTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='pymatgen-api-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(self.root, {'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
            'pymatgen_worker_token': 'synthetic-pymatgen-worker-token-12345678', 'pymatgen_enabled': True,
            'abtem_worker_token': 'synthetic-abtem-worker-token-12345678', 'abtem_enabled': True})
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.headers = {'X-CSRF-Token': 'synthetic-csrf', 'Origin': 'http://testserver'}
        self.worker = {'Authorization': 'Bearer synthetic-pymatgen-worker-token-12345678'}
        with self.app.state.database.connection(write=True) as db:
            for identifier in ('u1', 'u2'):
                db.execute("INSERT INTO users VALUES (?,?,'unused',1,0,?)", (identifier, identifier + '@example.test', time.time()))
                from highscore_web.credits import Credits
                Credits.grant(db, identifier)
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (hashlib.sha256(identifier.encode()).hexdigest(), identifier, 'synthetic-csrf', time.time() + 3600))
        self.client.cookies.set('highscore_session', 'u1')

    def submit(self, key='request-1', **values):
        return self.client.post(BASE + '/jobs', headers=self.headers,
            json={'request_key': key, 'operation': 'convert', 'inputs': [upload()], 'parameters': {}, 'pricing_version': 1, **values})

    def claim(self):
        response = self.client.post(WORKER + '/claim', headers=self.worker, json={'worker_id': 'synthetic-worker', 'available': True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def complete(self, claim, state='succeeded', **values):
        if state == 'succeeded' and 'bundle_base64' not in values:
            values['bundle_base64'] = base64.b64encode(sample_bundle()).decode('ascii')
        return self.client.post(WORKER + '/jobs/' + claim['job']['id'] + '/complete', headers=self.worker,
            json={'lease_token': claim['lease_token'], 'state': state, **values})

    def ledger(self):
        with self.app.state.database.connection() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM credit_entries WHERE kind<>'registration' ORDER BY rowid")]

    def test_paid_success_repeat_download_and_receipt_charge_only_once(self):
        self.assertEqual(self.ledger(), [])
        response = self.submit(); self.assertEqual(response.status_code, 200, response.text)
        job = response.json(); claim = self.claim()
        self.assertEqual(claim['job']['inputs'][0]['sha256'], hashlib.sha256(CIF).hexdigest())
        for _ in range(2):
            response = self.complete(claim); self.assertEqual(response.status_code, 200, response.text)
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertTrue(final['download_available']); self.assertEqual(final['summary']['atom_count'], 1)
        self.assertEqual(final['kind'], 'pymatgen')
        self.assertEqual(final['credits'], {'reserved': 1, 'charged': 1, 'status': 'charged'})
        before = self.ledger()
        for _ in range(2):
            response = self.client.get(BASE + '/jobs/' + job['id'] + '/download')
            self.assertEqual(response.status_code, 200, response.text)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertTrue(all('/' not in name for name in archive.namelist()))
                self.assertEqual(archive.read('input.cif'), CIF)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(len(before), 2)

    def test_request_id_binds_operation_parameters_input_name_and_bytes(self):
        first = self.submit().json()
        self.assertEqual(self.submit().json()['id'], first['id'])
        for changed in ({'inputs': [upload(CIF + b'\n')]}, {'inputs': [upload(filename='其他.cif')]},
                        {'operation': 'analyze'}, {'operation': 'compare', 'inputs': [upload(), upload()]}):
            self.assertEqual(self.submit(**changed).status_code, 409)
        self.assertEqual(self.submit('another-key').status_code, 409)
        self.assertEqual(self.submit(parameters={'unknown': 1}).status_code, 422)
        self.assertEqual(len(self.ledger()), 1)

    def test_owner_result_returns_verified_full_result_and_parameters_without_extra_charging(self):
        job = self.submit().json(); claim = self.claim()
        self.assertEqual(self.complete(claim).status_code, 200)
        before = self.client.get(BASE + '/jobs/' + job['id']).json()
        with zipfile.ZipFile(io.BytesIO(sample_bundle())) as archive:
            expected = json.loads(archive.read('result.json'))
        with patch('highscore_web.credits.Credits.grant', side_effect=AssertionError('读取结果不得赠分')), \
                patch('highscore_web.credits.Credits.entry', side_effect=AssertionError('读取结果不得写账')):
            for _ in range(2):
                response = self.client.get(BASE + '/jobs/' + job['id'] + '/result')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {'result': expected, 'parameters': expected['parameters']})
                model = response.json()['result']['structures'][0]
                self.assertEqual(model['lattice_parameters'], [5.43, 5.43, 5.43, 90., 90., 90.])
                self.assertEqual(model['fractional_positions'], [[0., 0., 0.]])
                self.assertAlmostEqual(model['volume_angstrom3'], 5.43 ** 3)
                self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id']).json(), before)
        self.assertEqual(len(self.ledger()), 2)

    def test_result_requires_owner_verified_session_success_and_unexpired_intact_bundle(self):
        job = self.submit().json()
        url = BASE + '/jobs/' + job['id'] + '/result'
        self.assertEqual(self.client.get(url).status_code, 410)
        claim = self.claim()
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertEqual(self.complete(claim).status_code, 200)
        self.client.cookies.set('highscore_session', 'u2')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.cookies.set('highscore_session', 'u1')
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET verified=0 WHERE id='u1'")
        self.assertEqual(self.client.get(url).status_code, 401)
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET verified=1 WHERE id='u1'")
        path = self.root / 'output/pymatgen' / job['id'] / 'result.zip'
        original = path.read_bytes()
        path.write_bytes(original + b'tampered')
        self.assertEqual(self.client.get(url).status_code, 410)
        path.write_bytes(original)
        with patch('highscore_web.pymatgen_store.validate_bundle', side_effect=ValueError('受控跨格式复验失败')):
            self.assertEqual(self.client.get(url).status_code, 410)
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE pymatgen_jobs SET download_expires_at=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.ledger()), 2)

    def test_all_operations_validate_count_and_limits_and_refund_on_cancel(self):
        for index, operation in enumerate(('analyze', 'convert', 'xrd', 'supercell', 'compare')):
            inputs = [upload(), upload(filename='第二.cif')] if operation == 'compare' else [upload()]
            response = self.submit(str(index), operation=operation, inputs=inputs)
            self.assertEqual(response.status_code, 200, response.text)
            self.client.post(BASE + '/jobs/' + response.json()['id'] + '/cancel', headers=self.headers, json={})
        self.assertEqual(self.submit('bad-count', operation='compare').status_code, 422)
        self.assertEqual(self.submit('bad-scaling', operation='supercell', parameters={'scaling': [5, 2, 2]}).status_code, 422)
        self.assertEqual(len(self.ledger()), 10)

    def test_ownership_verified_login_csrf_origin_and_independent_worker_token(self):
        job = self.submit().json()
        self.client.cookies.set('highscore_session', 'u2')
        for suffix in ('', '/download', '/image/xrd'):
            self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + suffix).status_code, 404)
        self.assertEqual(self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers).status_code, 404)
        self.client.cookies.set('highscore_session', 'u1')
        self.assertEqual(self.client.post(BASE + '/jobs', json={}).status_code, 403)
        self.assertEqual(self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers={**self.headers, 'Origin': 'https://other.test'}).status_code, 403)
        self.assertEqual(self.client.post(WORKER + '/claim', headers={'Authorization': 'Bearer synthetic-abtem-worker-token-12345678'}, json={'worker_id': 'other', 'available': True}).status_code, 401)
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET verified=0 WHERE id='u1'")
        self.assertEqual(self.client.get(BASE + '/status').status_code, 401)

    def test_queued_cancel_and_running_cancel_require_stopped_confirmation(self):
        job = self.submit().json()
        self.assertEqual(self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers).json()['state'], 'cancelled')
        job = self.submit('running').json(); claim = self.claim()
        cancelled = self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers).json()
        self.assertEqual(cancelled['state'], 'running'); self.assertTrue(cancelled['cancel_requested'])
        self.assertEqual(self.complete(claim).status_code, 409)
        for _ in range(2):
            self.assertEqual(self.complete(claim, 'cancelled').status_code, 200)
        self.assertFalse((self.root / 'output/pymatgen' / job['id']).exists())
        self.assertEqual(len(self.ledger()), 4)

    def test_queue_and_lease_timeouts_do_not_publish_and_refund_once(self):
        job = self.submit().json()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE pymatgen_jobs SET queue_deadline=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id']).json()['state'], 'timed_out')
        job = self.submit('lease').json(); claim = self.claim()
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE pymatgen_jobs SET lease_until=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertEqual(self.complete(claim).status_code, 409)
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id']).json()['state'], 'timed_out')
        self.assertEqual(len(self.ledger()), 4)

    def test_partial_tampered_or_changed_results_rejected_and_original_never_overwritten(self):
        job = self.submit().json(); claim = self.claim()
        self.assertEqual(self.complete(claim, bundle_base64=base64.b64encode(b'half-zip').decode()).status_code, 422)
        with zipfile.ZipFile(io.BytesIO(sample_bundle())) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files['sites.csv'] += b'tamper'
        changed = io.BytesIO()
        with zipfile.ZipFile(changed, 'w') as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        self.assertEqual(self.complete(claim, bundle_base64=base64.b64encode(changed.getvalue()).decode()).status_code, 422)
        self.assertFalse((self.root / 'output/pymatgen' / job['id']).exists())
        self.assertEqual(self.complete(claim).status_code, 200)
        path = self.root / 'output/pymatgen' / job['id'] / 'result.zip'
        original = path.read_bytes()
        self.assertEqual(self.complete(claim, bundle_base64=base64.b64encode(sample_bundle(seconds=.2)).decode()).status_code, 409)
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(original + b'tamper')
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 410)
        self.assertFalse(self.client.get(BASE + '/jobs/' + job['id']).json()['download_available'])

    def test_expiry_removes_raw_input_and_file_keeps_task_and_settled_ledger(self):
        job = self.submit().json(); claim = self.claim(); self.complete(claim)
        with self.app.state.database.connection(write=True) as db:
            db.execute('UPDATE pymatgen_jobs SET download_expires_at=? WHERE id=?', (time.time() - 1, job['id']))
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertTrue(final['file_expired']); self.assertEqual(final['state'], 'succeeded')
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 410)
        with self.app.state.database.connection() as db:
            self.assertIsNone(db.execute('SELECT content FROM pymatgen_inputs WHERE job_id=?', (job['id'],)).fetchone()[0])
        self.assertEqual(self.submit().json()['id'], job['id'])
        self.assertEqual(len(self.ledger()), 2)

    def test_concurrent_success_publishes_one_package_and_one_settled_audit(self):
        job = self.submit().json(); claim = self.claim()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.complete(claim), range(2)))
        self.assertTrue(all(reply.status_code == 200 for reply in results), [reply.text for reply in results])
        with self.app.state.database.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='pymatgen_settled'").fetchone()[0], 1)
        self.assertEqual(list((self.root / 'output/pymatgen').iterdir()), [self.root / 'output/pymatgen' / job['id']])
        self.assertEqual(len(self.ledger()), 2)

    def test_slow_result_validation_allows_cancel_and_cannot_publish_after_cancel(self):
        job = self.submit().json(); claim = self.claim()
        from highscore_web.pymatgen_store import validate_bundle
        entered, resume = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(15))
            return validate_bundle(*args, **kwargs)
        with patch('highscore_web.pymatgen_store.validate_bundle', side_effect=slow):
            with ThreadPoolExecutor(max_workers=2) as pool:
                completed = pool.submit(self.complete, claim)
                self.assertTrue(entered.wait(15))
                cancelled = self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers)
                self.assertEqual(cancelled.status_code, 200)
                resume.set()
                self.assertEqual(completed.result().status_code, 409)
        self.assertFalse((self.root / 'output/pymatgen' / job['id']).exists())
        self.assertEqual(self.complete(claim, 'cancelled').status_code, 200)
        self.assertEqual(len(self.ledger()), 2)

    def test_publish_failure_leaves_no_half_package_and_same_lease_can_retry(self):
        job = self.submit().json(); claim = self.claim()
        with patch.object(Path, 'rename', side_effect=OSError('受控同文件系统发布失败')):
            response = self.complete(claim)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertFalse((self.root / 'output/pymatgen' / job['id']).exists())
        self.assertEqual(list((self.root / 'output/pymatgen').iterdir()), [])
        self.assertEqual(self.client.get(BASE + '/jobs/' + job['id']).json()['state'], 'running')
        self.assertEqual(self.complete(claim).status_code, 200)
        self.assertEqual(len(self.ledger()), 2)

    def test_archive_source_requires_owner_receipts_and_replay_uses_accepted_snapshot(self):
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
        listed = self.client.get(BASE + '/sources?limit=1').json()
        self.assertEqual([(item['job_id'], item['code']) for item in listed['items']], [('synthetic-archive', 'ICSD-000001')])
        origin = {'type': 'archive', 'job_id': 'synthetic-archive', 'code': 'ICSD-000001'}
        submitted = self.submit(inputs=[origin]); self.assertEqual(submitted.status_code, 200, submitted.text)
        self.client.post(BASE + '/jobs/' + submitted.json()['id'] + '/cancel', headers=self.headers)
        self.client.cookies.set('highscore_session', 'u2')
        self.assertEqual(self.client.get(BASE + '/sources').json()['items'], [])
        self.assertEqual(self.submit(inputs=[origin]).status_code, 404)
        self.client.cookies.set('highscore_session', 'u1')
        cif_path = self.root / relative / receipt['files']['cif']['path']
        cif_path.write_bytes(cif_path.read_bytes() + b'\n# tampered\n')
        self.assertEqual(self.submit(inputs=[origin]).json()['id'], submitted.json()['id'])
        self.assertEqual(self.submit('new-request', inputs=[origin]).status_code, 409)
        self.assertEqual(self.client.get(BASE + '/sources').json()['items'], [])
        self.assertEqual(len(self.ledger()), 2)

    def test_only_exact_submit_and_complete_routes_have_large_body_limits(self):
        from test_admin_body_limits import AdminBodyLimitTests
        invoke = AdminBodyLimitTests.invoke
        for declared in (False, True):
            for path in (BASE + '/jobs', WORKER + '/jobs/' + 'a' * 32 + '/complete'):
                self.assertEqual(invoke(self, path, b' ' * 800000, declared=declared)[0], 204)
            for path in (BASE + '/jobs-extra', BASE + '/jobs/id/cancel', WORKER + '/jobs/' + 'a' * 32 + '/heartbeat'):
                self.assertEqual(invoke(self, path, b' ' * 20001, declared=declared), (413, []))
            self.assertEqual(invoke(self, BASE + '/jobs', b' ' * (1400 * 1024 + 1), declared=declared), (413, []))


if __name__ == '__main__':
    unittest.main()
