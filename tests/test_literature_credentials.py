"""账号密文、来源保留与安全失败；不包含真实账号。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LiteratureCredentialTests(unittest.TestCase):
    def setUp(self):
        try:
            from highscore_literature.credentials import save_accounts,load_accounts
        except ImportError:
            self.fail('尚未实现文献账号加密配置')
        self.save,self.load=save_accounts,load_accounts
        directory=ROOT/'.cache/tmp'
        directory.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=directory,prefix='literature-credentials-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.accounts=[{'username':'synthetic@example.test','password':'synthetic-private-password','enabled':True}]

    def test_roundtrip_encrypted_separate_key_and_no_stdout(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.save(self.root,self.accounts)
            values=self.load(self.root)
        self.assertEqual(values[0]['password'],self.accounts[0]['password'])
        self.assertEqual(output.getvalue(),'')
        paths=list((self.root/'runtime/literature-credentials').iterdir())
        self.assertEqual(len(paths),2)
        for path in paths:
            self.assertNotIn(self.accounts[0]['password'].encode(),path.read_bytes())
            if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_missing_key_never_replaces_existing_ciphertext(self):
        self.save(self.root,self.accounts)
        key=self.root/'runtime/literature-credentials/accounts.key'
        target=key.with_name('accounts.encrypted.json')
        before=target.read_bytes()
        key.unlink()
        with self.assertRaisesRegex(ValueError,'密钥'):
            self.save(self.root,self.accounts)
        self.assertFalse(key.exists())
        self.assertEqual(target.read_bytes(),before)

    def test_corrupted_cipher_fails_without_secret_in_exception(self):
        self.save(self.root,self.accounts)
        target=self.root/'runtime/literature-credentials/accounts.encrypted.json'
        target.write_text('{"ciphertext":"wrong"}',encoding='utf-8')
        with self.assertRaises(ValueError) as caught:self.load(self.root)
        self.assertNotIn('synthetic-private-password',str(caught.exception))

    def test_duplicate_account_references_are_rejected_without_replacing_configuration(self):
        self.save(self.root,self.accounts)
        target=self.root/'runtime/literature-credentials/accounts.encrypted.json'
        before=target.read_bytes()
        accounts=[{**self.accounts[0],'account_ref':'same-ref'},
                  {'username':'second@example.test','password':'synthetic-second','account_ref':'same-ref'}]
        with self.assertRaises(ValueError):self.save(self.root,accounts)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(len(self.load(self.root)),1)

    def test_loading_ambiguous_encrypted_account_references_is_rejected(self):
        from cryptography.fernet import Fernet
        self.save(self.root,self.accounts)
        directory=self.root/'runtime/literature-credentials'
        payload={'accounts':[{**self.accounts[0],'account_ref':'same-ref'},
                             {'username':'second@example.test','password':'synthetic-second','account_ref':'same-ref'}]}
        ciphertext=Fernet((directory/'accounts.key').read_bytes()).encrypt(json.dumps(payload).encode()).decode()
        (directory/'accounts.encrypted.json').write_text(json.dumps({'schema_version':1,'ciphertext':ciphertext}),encoding='utf-8')
        with self.assertRaises(ValueError):self.load(self.root)


if __name__=='__main__':unittest.main()
