"""材料工具共用账本、价格快照与旧免费任务兼容。"""
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
import base64
import io
import json
import zipfile

from highscore_web.credits import Credits
from highscore_web.pymatgen_store import PymatgenStore
import test_pymatgen_api as api_fixture
from test_pymatgen_api import BASE, upload


def xrd_bundle():
    from highscore_pymatgen.artifacts import build_bundle, report_text, xrd_csv, xrd_graphs, json_bytes
    from highscore_pymatgen.contract import validate_parameters, extinction_error_recipe, XRD_TWO_THETA_TOLERANCE
    from test_pymatgen_contract import CIF
    with zipfile.ZipFile(io.BytesIO(api_fixture.sample_bundle())) as archive:
        result = json.loads(archive.read('result.json'))
        files = {name: archive.read(name) for name in ('structure.cif', 'structure.poscar', 'structure.json', 'sites.csv')}
    parameters = validate_parameters('xrd', {})
    details = {'roundtrip_verified': True, 'peaks': [], 'symprec': 0, 'intensity_model': 'static_independent_atoms_lorentz_polarization',
        'two_theta_merge_tolerance_degree': XRD_TWO_THETA_TOLERANCE,
        'peak_retention_relative_to_range_maximum_percent': 0.0,
        'numerical_extinction_filter': {'recipe': extinction_error_recipe(1), 'applied_relative_cutoff_percent': 0.0,
            'checked_reflections': 0, 'considered_peaks': 0, 'discarded_peak_count': 0,
            'discarded_peak_details_limit': 256, 'discarded_peaks': [], 'discarded_peak_details_truncated': False,
            'minimum_amplitude_error_bound': 0.0, 'maximum_amplitude_error_bound': 0.0}}
    result.update(operation='xrd', parameters=parameters, details=details)
    from highscore_pymatgen.artifacts import make_summary
    result['summary'] = make_summary('xrd', result['structures'], details)
    files.update({'报告.txt': report_text(result), 'xrd.json': json_bytes(details), 'xrd.csv': xrd_csv(details), **xrd_graphs(details, parameters)})
    return base64.b64encode(build_bundle([{'filename': '结构.cif', 'content': CIF}], 'xrd', parameters, result, files)).decode('ascii')


class MaterialCreditTests(unittest.TestCase):
    setUp = api_fixture.PymatgenApiTests.setUp
    submit = api_fixture.PymatgenApiTests.submit
    claim = api_fixture.PymatgenApiTests.claim
    complete = api_fixture.PymatgenApiTests.complete

    def balance(self, amount=10):
        with self.app.state.database.connection(write=True) as db:
            Credits.grant(db, 'u1')
            current = Credits.wallet(db, 'u1')['available']
            if current != amount:
                Credits.entry(db, 'u1', 'admin_adjustment', amount - current, 0, 'synthetic-balance')

    def wallet(self):
        with self.app.state.database.connection() as db:
            return Credits.wallet(db, 'u1')

    def test_each_operation_freezes_its_server_price_and_cancel_refunds_once(self):
        self.balance()
        prices = {'analyze': 1, 'convert': 1, 'xrd': 2, 'supercell': 1, 'compare': 1}
        self.assertEqual(self.client.get(BASE + '/status').json()['costs'], prices)
        for operation, price in prices.items():
            with self.subTest(operation=operation):
                sources = [upload(), upload()] if operation == 'compare' else [upload()]
                reply = self.submit(operation, operation=operation, inputs=sources)
                self.assertEqual(reply.status_code, 200, reply.text)
                job = reply.json()
                self.assertEqual(job['credits'], {'reserved': price, 'charged': 0, 'status': 'frozen'})
                self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10 - price, price))
                self.assertEqual(self.submit(operation, operation=operation, inputs=sources).json()['id'], job['id'])
                for _ in range(2):
                    cancelled = self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers).json()
                    self.assertEqual(cancelled['credits'], {'reserved': price, 'charged': 0, 'status': 'refunded'})
                self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (10, 0))

    def test_insufficient_credit_creates_no_task_inputs_or_ledger_entry(self):
        for available in (0, 1):
            self.balance(available)
            before = self.wallet()['items']
            response = self.submit('insufficient-' + str(available), operation='xrd')
            self.assertEqual(response.status_code, 402, response.text)
            self.assertIn('2', response.json()['detail'])
            self.assertEqual(self.wallet()['items'], before)
            with self.app.state.database.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM pymatgen_jobs').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM pymatgen_inputs').fetchone()[0], 0)
            # 每次调整使用独立的测试幂等键。
            with self.app.state.database.connection(write=True) as db:
                db.execute("UPDATE credit_entries SET event_key=event_key||? WHERE event_key='synthetic-balance'", (str(available),))

    def test_stale_free_page_and_client_prices_cannot_create_charged_tasks(self):
        self.balance()
        before = self.wallet()
        self.assertEqual(self.submit(pricing_version=None).status_code, 409)
        self.assertEqual(self.submit(pricing_version=2).status_code, 422)
        for value in ({'cost': 0}, {'credits': {'reserved': 0}}):
            self.assertEqual(self.submit(**value).status_code, 422)
        self.assertEqual(self.wallet(), before)
        self.assertEqual(self.client.get(BASE + '/jobs').json()['items'], [])

    def test_verified_success_charges_once_and_downloads_do_not_charge(self):
        self.balance(1)
        job = self.submit().json(); claim = self.claim()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.complete(claim), range(2)))
        self.assertTrue(all(reply.status_code == 200 for reply in replies), [r.text for r in replies])
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(final['credits'], {'reserved': 1, 'charged': 1, 'status': 'charged'})
        before = self.wallet()
        for _ in range(2):
            self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/download').status_code, 200)
            self.assertEqual(self.client.get(BASE + '/jobs/' + job['id'] + '/result').status_code, 200)
        self.assertEqual(self.wallet(), before)
        self.assertEqual((before['available'], before['frozen']), (0, 0))
        entries = [item for item in before['items'] if item['job_id'] == job['id']]
        self.assertEqual(sorted(item['kind'] for item in entries), ['pymatgen_charge', 'pymatgen_freeze'])
        self.assertTrue(all(item['job_kind'] == 'pymatgen' for item in entries))
        self.assertTrue(all('材料工具' in item['label'] for item in entries))

    def test_bad_bundle_keeps_hold_then_failure_refunds_original_xrd_price(self):
        self.balance(2)
        job = self.submit(operation='xrd').json(); claim = self.claim()
        self.assertEqual(self.complete(claim, bundle_base64='aGFsZg==').status_code, 422)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (0, 2))
        for _ in range(2):
            self.assertEqual(self.complete(claim, 'failed', error_code='unsupported_occupancy').status_code, 200)
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(final['credits'], {'reserved': 2, 'charged': 0, 'status': 'refunded'})
        self.assertIn('2', final['message'])
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (2, 0))

    def test_xrd_success_charges_two_after_full_bundle_validation(self):
        self.balance(2)
        job = self.submit(operation='xrd').json(); claim = self.claim()
        bundle = xrd_bundle()
        for _ in range(2):
            reply = self.complete(claim, bundle_base64=bundle)
            self.assertEqual(reply.status_code, 200, reply.text)
        final = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(final['credits'], {'reserved': 2, 'charged': 2, 'status': 'charged'})
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (0, 0))

    def test_running_cancel_refunds_only_after_stopped_receipt(self):
        self.balance(2)
        job = self.submit(operation='xrd').json(); claim = self.claim()
        cancelled = self.client.post(BASE + '/jobs/' + job['id'] + '/cancel', headers=self.headers).json()
        self.assertEqual(cancelled['credits']['status'], 'frozen')
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (0, 2))
        for _ in range(2):
            self.assertEqual(self.complete(claim, 'cancelled').status_code, 200)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (2, 0))

    def test_queue_and_lease_expiry_refund_once(self):
        self.balance(2)
        for mode in ('queue_deadline', 'lease_until'):
            job = self.submit(mode, operation='xrd').json()
            if mode == 'lease_until':
                self.claim()
            with self.app.state.database.connection(write=True) as db:
                db.execute('UPDATE pymatgen_jobs SET ' + mode + '=? WHERE id=?', (time.time() - 1, job['id']))
            for _ in range(2):
                final = self.client.get(BASE + '/jobs/' + job['id']).json()
                self.assertEqual(final['credits']['status'], 'refunded')
            self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (2, 0))

    def test_cross_feature_simultaneous_reservations_cannot_overspend(self):
        from test_abtem_store import upload as abtem_upload
        self.balance(2)
        database = self.app.state.database
        material, abtem = self.app.state.pymatgen, self.app.state.abtem
        with database.connection() as db:
            prepared = material.resolve_inputs(db, 'u1', [upload()], 'xrd', {})
            microscope = abtem.input(db, 'u1', abtem_upload(), {})
        def enqueue(kind):
            try:
                with database.connection(write=True) as db:
                    service, source = (material, prepared) if kind == 'material' else (abtem, microscope)
                    service.enqueue(db, 'u1', kind, source)
                return 200
            except ValueError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(enqueue, ('material', 'abtem')))
        self.assertEqual(sorted(states), [200, 402])
        wallet = self.wallet()
        self.assertGreaterEqual(wallet['available'], 0)
        self.assertEqual(wallet['available'] + wallet['frozen'], 2)

    def test_legacy_queue_migrates_without_backcharge_or_fake_refund(self):
        self.balance(10)
        job = self.submit().json()
        with self.app.state.database.connection(write=True) as db:
            # 模拟升级前无计费列、无材料账本的真实旧队列。
            db.execute('DELETE FROM credit_entries WHERE job_id=?', (job['id'],))
            for name in ('reserved_credits', 'credit_status'):
                db.execute('ALTER TABLE pymatgen_jobs DROP COLUMN ' + name)
        before = self.wallet()
        for _ in range(2):
            PymatgenStore(self.root, self.app.state.database, enabled=True)
        old = self.client.get(BASE + '/jobs/' + job['id']).json()
        self.assertEqual(old['credits'], {'reserved': 0, 'charged': 0, 'status': 'not_required'})
        self.assertNotIn('退回', old['message'])
        claim = self.claim()
        self.assertEqual(self.complete(claim).status_code, 200)
        self.assertEqual(self.wallet(), before)
        self.assertEqual(self.submit().json()['id'], job['id'])
        self.assertEqual(self.wallet(), before)


if __name__ == '__main__':
    unittest.main()
