"""在项目内创建模拟角色独立凭据，绝不输出秘密。"""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description='创建独立电镜模拟配置')
    parser.add_argument('--root', default='.')
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    destination = root / 'runtime/abtem.env'
    if destination.exists():
        raise SystemExit('配置已存在，保留原独立令牌；请显式编辑开启状态。')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = 'HIGHSCORE_ABTEM_ENABLED=' + ('true' if args.enable else 'false') + '\n'
    content += 'HIGHSCORE_ABTEM_WORKER_TOKEN=' + secrets.token_urlsafe(48) + '\n'
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(content)
        stream.flush(); os.fsync(stream.fileno())
    print('已创建 ./runtime/abtem.env，秘密未输出。')


if __name__ == '__main__':
    main()
