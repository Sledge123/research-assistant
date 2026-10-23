"""Linux 邮箱配置必须加密，损坏密钥不能被静默替换。"""
import importlib
from pathlib import Path
import tempfile
import unittest

from highscore_downloader.mail_settings import MailSettings

ROOT = Path(__file__).resolve().parent.parent


class LinuxMailConfigTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache/tmp').mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / '.cache/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def module(self):
        self.assertTrue((ROOT / 'highscore_database/mail_config.py').is_file(), '尚未实现Linux加密邮件配置')
        return importlib.import_module('highscore_database.mail_config')

    def test_encrypt_roundtrip_and_no_plain_secret(self):
        module = self.module()
        settings = MailSettings(username='synthetic@example.com', authorization_code='synthetic-private-secret')
        module.save_settings(self.root, settings)
        loaded = module.load_settings(self.root)
        self.assertEqual(loaded.authorization_code, settings.authorization_code)
        self.assertNotIn(settings.authorization_code, repr(loaded))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(settings.authorization_code.encode(), path.read_bytes())

    def test_missing_and_corrupt_key_fail_without_replacing(self):
        module = self.module()
        with self.assertRaises(ValueError):
            module.load_settings(self.root)
        settings = MailSettings(username='synthetic@example.com', authorization_code='synthetic-private-secret')
        module.save_settings(self.root, settings)
        key = self.root / 'runtime/mail.key'
        key.write_bytes(b'invalid-key')
        with self.assertRaises(ValueError):
            module.load_settings(self.root)
        with self.assertRaises(ValueError):
            module.save_settings(self.root, settings)
        self.assertEqual(key.read_bytes(), b'invalid-key')

    def test_paths_and_link_parent_cannot_escape_root(self):
        module = self.module()
        with self.assertRaises(ValueError):
            module.load_settings(self.root, '../mail.json')
