"""模拟队列、共用积分与发布恢复的真实 SQLite 测试。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from highscore_abtem.artifacts import build_bundle
from highscore_abtem.contract import DEFAULT_PARAMETERS
from highscore_web.abtem_store import AbtemError, AbtemStore
from highscore_web.credits import CreditError, Credits
from highscore_web.database import Database
from highscore_web.literature_store import LiteratureError, LiteratureStore

ROOT = Path(__file__).resolve().parents[1]
CIF = b'''data_synthetic_Si
_cell_length_a 5.43
_cell_length_b 5.43
_cell_length_c 5.43
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
_space_group_symop_operation_xyz
'x,y,z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Si1 Si 0 0 0 1
'''


def sample_bundle(cif=CIF, parameters=None, *, factor=1.0):
    parameters = dict(parameters or DEFAULT_PARAMETERS)
    model = {'cell_angstrom': [10, 10, parameters['thickness_nm'] * 10], 'symbols': ['Si'],
             'fractional_positions': [[.5, .5, .5]], 'pbc': [True, True, False],
             'zone_axis': parameters['zone_axis'], 'occupancy': 'full', 'thermal_model': 'static'}
    data = struct.pack('<64f', *(factor * (index + 1) for index in range(64)))
    measurements = {
        'saed': {'shape': [8, 8], 'data': data, 'x_start': -.4, 'y_start': -.4,
                 'x_step': .1, 'y_step': .1, 'coordinate_unit': '1/angstrom'},
        'hrtem': {'shape': [8, 8], 'data': data, 'x_start': 0, 'y_start': 0,
                  'x_step': 1.25, 'y_step': 1.25, 'coordinate_unit': 'angstrom'}}
    return build_bundle(cif, parameters, model, measurements, {'abtem': '1.0.10'},
                        {'seconds': 1.0, 'peak_rss_bytes': 1000000})


def upload(cif=CIF):
    return {'type': 'upload', 'filename': 'synthetic.cif',
            'cif_base64': base64.b64encode(cif).decode('ascii')}


class AbtemStoreTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='abtem-store-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = Database(self.root)
        with self.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES ('u1','synthetic@example.test','unused',1,0,?)", (time.time(),))
        self.service = AbtemStore(self.root, self.database, enabled=True)
        with self.database.connection() as db:
            self.input = self.service.input(db, 'u1', upload(), {})

    def submit(self, key='request-1'):
        with self.database.connection(write=True) as db:
            return self.service.enqueue(db, 'u1', key, self.input)

    def claim(self):
        with self.database.connection(write=True) as db:
            return self.service.claim(db, 'synthetic-worker', True)

    def wallet(self):
        with self.database.connection() as db:
            return Credits.wallet(db, 'u1')

    def test_schema_initialization_does_not_change_legacy_users_or_tables(self):
        with self.database.connection() as db:
            before = tuple(db.execute('SELECT * FROM users').fetchone())
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        AbtemStore(self.root, self.database, enabled=False)
        with self.database.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM users').fetchone()), before)
            self.assertEqual(len(db.execute('PRAGMA table_info(users)').fetchall()), 6)
            self.assertEqual(db.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0], schema)

    def test_cross_abtem_literature_xrd_concurrency_cannot_spend_same_point(self):
        literature = LiteratureStore(self.root, self.database)
        with self.database.connection(write=True) as db:
            Credits.grant(db, 'u1')
            Credits.entry(db, 'u1', 'admin_adjustment', -9, 0, 'synthetic-balance')
            db.execute("UPDATE settings SET value='true' WHERE key='literature_enabled'")

        def abtem():
            try:
                self.submit()
                return True
            except AbtemError:
                return False

        def paper():
            try:
                with self.database.connection(write=True) as db:
                    literature.enqueue(db, 'u1', 'Synthetic cross business atomic balance', 'paper-1')
                return True
            except LiteratureError:
                return False

        def xrd():
            try:
                with self.database.connection(write=True) as db:
                    db.execute("INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,created_at,updated_at) VALUES ('xrd-1','u1','archive','queued','{}','synthetic','xrd-1',?,?)", (time.time(), time.time()))
                    Credits.reserve_xrd(db, 'u1', 'xrd-1', 1)
                return True
            except CreditError:
                return False

        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda action: action(), (abtem, paper, xrd)))
        self.assertEqual(sum(values), 1)
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (0, 1))

    def test_success_publication_can_resume_after_database_rollback(self):
        job = self.submit()
        claim = self.claim()
        encoded = base64.b64encode(sample_bundle()).decode('ascii')
        with self.database.connection() as db:
            row = self.service.leased(db, job['id'], claim['lease_token'])
        prepared = self.service.prepare(row, encoded)
        self.addCleanup(lambda: __import__('shutil').rmtree(prepared['directory'], ignore_errors=True))
        with self.assertRaisesRegex(RuntimeError, 'synthetic commit failure'):
            with self.database.connection(write=True) as db:
                self.service.complete(db, job['id'], claim['lease_token'], 'succeeded', prepared)
                raise RuntimeError('synthetic commit failure')
        destination = self.root / 'output/abtem' / job['id'] / 'result.zip'
        self.assertTrue(destination.is_file())
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 1))
        second = self.service.prepare(row, encoded)
        self.addCleanup(lambda: __import__('shutil').rmtree(second['directory'], ignore_errors=True))
        with self.database.connection(write=True) as db:
            self.service.complete(db, job['id'], claim['lease_token'], 'succeeded', second)
        with self.database.connection() as db:
            final = self.service.own(db, job['id'], 'u1')
            data, checked = self.service.read_bundle(final)
            self.assertEqual(len(checked['files']), 13)
            self.assertEqual(final['credit_status'], 'charged')
        self.assertEqual((self.wallet()['available'], self.wallet()['frozen']), (9, 0))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertTrue(all('/' not in name for name in archive.namelist()))

    def test_staging_is_on_result_filesystem_and_write_failure_not_published(self):
        job = self.submit(); claim = self.claim()
        with self.database.connection() as db:
            row = self.service.leased(db, job['id'], claim['lease_token'])
        encoded = base64.b64encode(sample_bundle()).decode('ascii')
        with patch('highscore_web.abtem_store.os.fsync', side_effect=OSError('synthetic full disk')):
            with self.assertRaises(AbtemError):
                self.service.prepare(row, encoded)
        parent = self.root / 'output/abtem'
        self.assertEqual(list(parent.iterdir()), [])
        prepared = self.service.prepare(row, encoded)
        self.addCleanup(lambda: __import__('shutil').rmtree(prepared['directory'], ignore_errors=True))
        self.assertEqual(prepared['directory'].parent, parent)
        self.assertEqual(prepared['path'].parent, prepared['directory'])


if __name__ == '__main__':
    unittest.main()
