"""在项目内生成仅文献提醒邮件执行器使用的 Linux 凭据。"""
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


def configure(root):
    project = Path(root).resolve()
    if not project.is_dir():
        raise ValueError('项目目录不存在。')
    runtime = project/'runtime'
    target = runtime/'literature-mail.env'
    # Keep the host-side bootstrap usable on older Linux Python builds where
    # pathlib.Path.is_relative_to and Path.unlink(missing_ok=...) are absent.
    if runtime.is_symlink() or runtime.resolve().parent != project:
        raise ValueError('运行目录无法核验。')
    runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.stat().st_mode & 0o077:
            raise ValueError('现有文献邮件凭据文件路径或权限无法核验，未覆盖。')
        text = target.read_text(encoding='utf-8')
        values = [line.partition('=')[2] for line in text.splitlines()
                  if line.startswith('LITERATURE_MAIL_WORKER_TOKEN=')]
        if len(values)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',values[0]):
            raise ValueError('现有文献邮件凭据格式无效，未覆盖。')
        return 'PASS 文献提醒邮件执行器凭据已存在且权限受限。'
    value = secrets.token_urlsafe(48)
    payload = f'LITERATURE_MAIL_WORKER_TOKEN={value}\n'.encode('ascii')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb',dir=runtime,prefix='.literature-mail-',suffix='.tmp',delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary,0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary,target)
        os.chmod(target,0o600)
    except FileExistsError:
        raise ValueError('文献邮件凭据文件已由另一进程创建，请重新运行检查。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return 'PASS 已在项目运行目录创建独立文献邮件凭据；内容未输出。'


if __name__=='__main__':
    try:
        print(configure(Path(__file__).resolve().parents[1]))
    except Exception:
        print('FAIL 文献提醒邮件凭据配置失败；未输出凭据内容。',file=sys.stderr)
        raise SystemExit(1)
