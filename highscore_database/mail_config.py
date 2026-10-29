"""纯 Linux SMTP 配置：项目内分文件密钥、认证加密与严格读取失败。"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from highscore_downloader.mail_settings import MailSettings, validate_mail_settings
from highscore_worker.config import project_path


KEY = 'runtime/mail.key'
CONFIG = 'runtime/mail.encrypted.json'


def _private_file(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError('邮箱密钥或配置文件不存在。')
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('邮箱密钥及配置只允许文件拥有者读取（权限 0600）。')


def _write_private(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.mail-config-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cipher(root, create=False):
    key = project_path(root, KEY)
    if not key.exists():
        if not create:
            raise ValueError('尚未配置服务器邮箱。')
        # 不得在已有密文丢失密钥后生成新密钥覆盖配置。
        if project_path(root, CONFIG).exists():
            raise ValueError('现有邮箱密文缺少对应密钥，请恢复备份。')
        key.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
    _private_file(key)
    try:
        return Fernet(key.read_bytes())
    except (OSError, ValueError):
        raise ValueError('邮箱密钥无法校验，未替换现有密钥。') from None


def save_settings(root, settings):
    settings = validate_mail_settings(settings, settings.recipient or settings.username)
    values = asdict(settings)
    values.pop('message', None)
    payload = json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8')
    cipher = _cipher(root, create=True)
    target = project_path(root, CONFIG)
    _write_private(target, json.dumps({'schema_version': 1, 'ciphertext': cipher.encrypt(payload).decode('ascii')}).encode('utf-8'))
    return settings


def load_settings(root, relative=CONFIG):
    try:
        target = project_path(root, relative)
        _private_file(target)
        envelope = json.loads(target.read_bytes())
        if not isinstance(envelope, dict) or envelope.get('schema_version') != 1 or set(envelope) != {'schema_version', 'ciphertext'}:
            raise ValueError()
        values = json.loads(_cipher(root).decrypt(envelope['ciphertext'].encode('ascii')))
        settings = MailSettings(**values)
        return validate_mail_settings(settings, settings.recipient or settings.username)
    except (OSError, ValueError, TypeError, InvalidToken, UnicodeError):
        raise ValueError('服务器邮箱配置无法读取或校验，请检查项目内密钥和加密配置；未返回秘密内容。') from None
