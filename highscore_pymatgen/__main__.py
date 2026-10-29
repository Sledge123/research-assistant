"""以项目 Python 源码启动独立晶体工具执行器。"""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='晶体工具执行器')
    parser.add_argument('--root', default='.')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('数值执行器仅在已核验的Linux隔离环境运行。')
    if not args.run:
        parser.error('请使用--run启动。')
    from .worker import run
    return run(Path(args.root), once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
