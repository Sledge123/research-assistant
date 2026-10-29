"""建立项目内独立角色配置；不返回或输出秘密。"""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description='配置独立材料工具角色，默认保持关闭')
    parser.add_argument('--root', default='.')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if os.name != 'posix':
        raise SystemExit('此配置入口用于Linux部署机。')
    for name in ('runtime/pymatgen-files', 'runtime/pymatgen-worker', 'runtime-cache/pymatgen',
                 'runtime-cache/pymatgen/tmp', 'runtime/science-lock'):
        path = root / name
        if path.is_symlink():
            raise SystemExit('角色目录不能为符号链接。')
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(str(path), 10001, 10001); os.chmod(str(path), 0o700)
    path = root / 'runtime/pymatgen.env'
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise SystemExit('既有角色配置权限无效。')
        values = dict(line.split('=', 1) for line in path.read_text().splitlines() if line)
        if (set(values) != {'HIGHSCORE_PYMATGEN_ENABLED', 'HIGHSCORE_PYMATGEN_WORKER_TOKEN'}
                or values['HIGHSCORE_PYMATGEN_ENABLED'] not in ('false', 'true')
                or len(values['HIGHSCORE_PYMATGEN_WORKER_TOKEN']) < 32):
            raise SystemExit('既有角色配置无效，请核对。')
        print('PASS 独立配置已存在，保持原配置。')
        return
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write('HIGHSCORE_PYMATGEN_ENABLED=false\nHIGHSCORE_PYMATGEN_WORKER_TOKEN=' + secrets.token_urlsafe(48) + '\n')
    print('PASS 独立配置已建立，功能保持关闭。')


if __name__ == '__main__':
    main()
