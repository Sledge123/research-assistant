"""常驻数据库和验证邮件执行器须配置为不同角色。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from highscore_worker.client import WorkerClient
from highscore_worker.config import WorkerConfig, WorkerError, load_config

ROOT = Path(__file__).resolve().parents[1]


class DatabaseWorkerRoleTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.cache/tmp'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base, prefix='worker-roles-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def config(self, **kwargs):
        return WorkerConfig(self.root, 'https://example.com/highscore/api', 'https://example.com/highscore/',
                            'synthetic-database', 'synthetic-db', 'synthetic-token', **kwargs)

    def test_database_role_does_not_claim_verification_mail(self):
        config = self.config(database_only=True)
        self.assertEqual(config.capabilities, ['search', 'archive', 'mail_retry'])
        client = WorkerClient(config)
        client._post = Mock(return_value={'job': None})
        client.claim()
        self.assertEqual(client._post.call_args.args[1]['capabilities'], config.capabilities)

    def test_roles_are_exclusive_and_require_boolean_values(self):
        for values in ({'database_only': True, 'mail_only': True}, {'database_only': 'true'}):
            with self.subTest(values=values), self.assertRaises(WorkerError):
                self.config(**values)
        self.assertEqual(self.config(mail_only=True).capabilities, ['verification_mail'])

    def test_database_role_can_be_loaded_without_secret_in_config_file(self):
        data = {'server_url': 'https://example.com/highscore/api', 'public_url': 'https://example.com/highscore/',
                'worker_id': 'synthetic-database', 'database_id': 'synthetic-db', 'database_only': True}
        (self.root / 'database-worker.local.json').write_text(json.dumps(data), encoding='utf-8')
        config = load_config(self.root, 'database-worker.local.json', environ={'HIGHSCORE_WORKER_TOKEN': 'synthetic-token'})
        self.assertTrue(config.database_only)
        self.assertNotIn('synthetic-token', repr(config))
