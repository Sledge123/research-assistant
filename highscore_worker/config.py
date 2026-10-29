"""项目内执行器设置；令牌不进入日志或公开接口。"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import urlsplit


class WorkerError(ValueError):
    pass


def project_path(root, value):
    if not isinstance(value, str) or not value or '\0' in value:
        raise WorkerError('配置路径必须是项目内相对路径。')
    path = Path(value)
    if path.anchor or PureWindowsPath(value).anchor or '..' in value.replace('\\', '/').split('/'):
        raise WorkerError('配置路径必须是项目内相对路径。')
    project = Path(root).resolve()
    target = project / path
    if not target.resolve().is_relative_to(project):
        raise WorkerError('配置路径不能超出项目。')
    for parent in (target, *target.parents):
        if parent == project:
            break
        if parent.is_symlink():
            raise WorkerError('配置路径不允许符号链接。')
    return target


def https_url(value, suffix):
    if not isinstance(value, str) or any(ch.isspace() for ch in value) or '\\' in value:
        raise WorkerError('服务地址必须是完整 HTTPS 地址。')
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or not url.path.endswith(suffix) or url.port not in (None, 443)):
            raise ValueError()
    except ValueError:
        raise WorkerError('服务地址必须是 HTTPS，且使用约定的网站路径。') from None
    return value


@dataclass(frozen=True)
class WorkerConfig:
    root: Path
    server_url: str
    public_url: str
    worker_id: str
    database_id: str
    token: str = field(repr=False)
    desktop_enabled: bool = False
    poll_seconds: float = 5
    executable: str | None = None
    mail_only: bool = False
    database_only: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'root', Path(self.root).resolve())
        if not self.root.is_dir():
            raise WorkerError('项目根目录不存在。')
        https_url(self.server_url, '/highscore/api')
        https_url(self.public_url, '/highscore/')
        if urlsplit(self.server_url).netloc.lower() != urlsplit(self.public_url).netloc.lower():
            raise WorkerError('API 和网站必须使用同一 HTTPS 主机。')
        for value in (self.worker_id, self.database_id):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', value):
                raise WorkerError('执行器和数据库标识仅允许 1 到 100 位字母、数字、点、下划线、短横线。')
        if not isinstance(self.token, str) or not self.token.strip() or any(ch.isspace() for ch in self.token):
            raise WorkerError('请在本地配置或 HIGHSCORE_WORKER_TOKEN 环境变量中设置执行器凭据。')
        if (type(self.desktop_enabled) is not bool or type(self.mail_only) is not bool
                or type(self.database_only) is not bool or self.mail_only and self.database_only
                or not 1 <= self.poll_seconds <= 60):
            raise WorkerError('执行器轮询配置无效。')
        if self.executable:
            project_path(self.root, self.executable)

    @property
    def capabilities(self):
        if self.mail_only:
            return ['verification_mail']
        if self.database_only:
            return ['search', 'archive', 'mail_retry']
        return ['search','archive','mail_retry','verification_mail']


def load_config(root, relative='worker.local.json', *, environ=None):
    environ = os.environ if environ is None else environ
    try:
        data = json.loads(project_path(root, relative).read_text(encoding='utf-8-sig'))
        allowed = {'server_url', 'public_url', 'worker_id', 'database_id', 'token',
                   'desktop_enabled', 'poll_seconds', 'executable', 'mail_only', 'database_only'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise WorkerError('执行器配置含未支持的字段。')
        data['token'] = environ.get('HIGHSCORE_WORKER_TOKEN') or data.get('token', '')
        return WorkerConfig(Path(root), **data)
    except WorkerError:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkerError('执行器本地配置无法读取或缺少必要字段。') from None
