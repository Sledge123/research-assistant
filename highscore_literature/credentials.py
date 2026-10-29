"""文献执行器私有账号配置；密钥和密文分别保存，任何失败不回显秘密。"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from cryptography.fernet import Fernet, InvalidToken

DIRECTORY = 'runtime/literature-credentials'


def private_path(root,name):
    root=Path(root).resolve()
    path=root/DIRECTORY/name
    if not path.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in [path,*path.parents] if parent.is_relative_to(root)):
        raise ValueError('账号配置路径无法核验。')
    return path


def _read(path):
    if not path.is_file() or path.is_symlink() or (os.name!='nt' and path.stat().st_mode&0o077):
        raise ValueError('账号密钥或密文不存在，或权限不是0600。')
    return path.read_bytes()


def _normalize(accounts):
    if not isinstance(accounts,list) or not 1<=len(accounts)<=50:
        raise ValueError('账号配置需要1至50个账号。')
    result=[]
    known=set()
    references=set()
    for account in accounts:
        if not isinstance(account,dict):raise ValueError('账号配置结构无效。')
        username,password=account.get('username'),account.get('password')
        if not isinstance(username,str) or not 1<=len(username.strip())<=254 or not isinstance(password,str) or not 1<=len(password)<=512:
            raise ValueError('账号或密码缺失，未返回秘密内容。')
        username=username.strip()
        if username.casefold() in known:continue
        known.add(username.casefold())
        reference=account.get('account_ref') or 'account-'+hashlib.sha256(username.casefold().encode()).hexdigest()[:16]
        if not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',reference):
            raise ValueError('账号引用格式无效。')
        if reference in references:
            raise ValueError('不同账号的引用重复，请检查配置；未覆盖已有账号。')
        references.add(reference)
        result.append({'account_ref':reference,'username':username,'password':password,'enabled':bool(account.get('enabled',True))})
    return result


def _write(path,payload):
    directory=path.parent
    directory.mkdir(parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=directory,prefix='.private-',delete=False) as stream:
            temporary=Path(stream.name)
            os.chmod(temporary,0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        os.chmod(path,0o600)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def save_accounts(root,accounts):
    values=_normalize(accounts)
    key=private_path(root,'accounts.key')
    target=private_path(root,'accounts.encrypted.json')
    if not key.exists():
        if target.exists():raise ValueError('已有账号密文缺少密钥，请恢复备份；未覆盖现有配置。')
        key.parent.mkdir(parents=True,exist_ok=True)
        os.chmod(key.parent,0o700)
        descriptor=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
    try:
        cipher=Fernet(_read(key))
        payload=json.dumps({'accounts':values},ensure_ascii=False,allow_nan=False).encode('utf-8')
        _write(target,json.dumps({'schema_version':1,'ciphertext':cipher.encrypt(payload).decode('ascii')}).encode('utf-8'))
    except (OSError,ValueError):
        raise ValueError('账号加密配置保存失败，请检查项目内密钥及权限；未返回秘密内容。') from None
    return len(values)


def load_accounts(root):
    try:
        key=private_path(root,'accounts.key')
        target=private_path(root,'accounts.encrypted.json')
        envelope=json.loads(_read(target))
        if set(envelope)!={'schema_version','ciphertext'} or envelope['schema_version']!=1:raise ValueError()
        payload=json.loads(Fernet(_read(key)).decrypt(envelope['ciphertext'].encode('ascii')))
        return [item for item in _normalize(payload['accounts']) if item['enabled']]
    except (OSError,ValueError,KeyError,TypeError,InvalidToken,UnicodeError):
        raise ValueError('账号密钥或加密配置无法校验，请安全恢复配置；未返回秘密内容。') from None
